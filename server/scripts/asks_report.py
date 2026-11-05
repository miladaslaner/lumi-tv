"""Where the time went, per ask from the TV: one row per ask (server stages joined with the TV's own timings by
ask_id), medians underneath, and any event-loop stalls in the same period.

    uv run python scripts/asks_report.py [--last 20] [--since 2026-11-04T10:00] [--log logs/lumitv.jsonl] [--picks]
                                         [--config lumitv.toml]

--picks prints what each answer showed and why (headline, subline, each pick's service and reason) instead.
The target line judges "end of the question -> posters drawn" against [budgets] ask_posters_p50_ms (and
ask_posters_p95_ms when the config has it) from --config, the same numbers `lumitv doctor` uses.

Columns (ms unless noted), in the order an ask goes:
  listen   TV: mic press -> "Listening" on screen        upload  server: receiving the spoken question's audio
  stt      server: speech-to-text                        heard   TV: end of speech -> "Looking" on screen
  start    Claude Code: turn start -> first tool call    find    find_titles
  think    find -> show_on_tv (Claude choosing)          show    show_on_tv
  api      Anthropic API time inside the turn (the rest of `total` is Claude Code + our tools)
  turns    model round trips                             total   whole Claude turn
  posters  TV: end of speech (or send) -> posters drawn
"""

import argparse
import json
import math
import statistics
import sys
import tomllib
from pathlib import Path

COLUMNS = [
    ("listen", "listen_ms", "tv"), ("upload", "upload_ms", "ask"), ("stt", "stt_ms", "ask"),
    ("heard", "heard_ms", "tv"), ("start", "claude_start_ms", "ask"), ("find", "find_ms", "ask"),
    ("think", "think_ms", "ask"), ("show", "show_ms", "ask"), ("api", "claude_api_ms", "ask"),
    ("turns", "claude_turns", "ask"), ("total", "claude_total_ms", "ask"), ("posters", "posters_ms", "tv"),
]  # fmt: skip


def read(path: Path, since: str | None) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if since is None or row.get("ts", "") >= since:
            rows.append(row)
    return rows


def join(rows: list[dict]) -> list[dict]:
    """One dict per ask: the `ask` line's fields plus `ask_timing`'s (prefixed tv: where they collide)."""
    asks: dict[str, dict] = {}
    for row in rows:
        aid = row.get("ask_id")
        if not aid or row.get("event") not in ("ask", "ask_timing"):
            continue
        entry = asks.setdefault(aid, {"ask_id": aid})
        if row["event"] == "ask":
            entry.update(row)
        else:
            entry["tv"] = row
    return [a for a in asks.values() if "event" in a]  # TV timings without a server line: not an ask we can explain


def cell(ask: dict, key: str, where: str):
    src = ask.get("tv", {}) if where == "tv" else ask
    v = src.get(key)
    return v if isinstance(v, (int, float)) else None


# "end of the question -> posters drawn" targets when the config names none: the example config's p50 and the p95
# this report has always used ([budgets] has no p95 key yet)
DEFAULT_P50_MS, DEFAULT_P95_MS = 12_000, 15_000


def read_budgets(config: Path) -> tuple[int, int]:
    """(p50, p95) targets in ms from the config's [budgets] (ask_posters_p50_ms, ask_posters_p95_ms); a missing
    file or key falls back to the defaults, a broken file is an error."""
    if not config.exists():
        print(f"no {config}: judging against the default targets", file=sys.stderr)
        return DEFAULT_P50_MS, DEFAULT_P95_MS
    with config.open("rb") as f:
        budgets = tomllib.load(f).get("budgets", {})
    p50, p95 = budgets.get("ask_posters_p50_ms", DEFAULT_P50_MS), budgets.get("ask_posters_p95_ms", DEFAULT_P95_MS)
    return int(p50), int(p95)


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..1) of a non-empty list: the value at rank ceil(q * n)."""
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))]


def target_line(posters_ms: list[float], p50_ms: int = DEFAULT_P50_MS, p95_ms: int = DEFAULT_P95_MS) -> str:
    """Whether asks met the response-time target: posters within p50_ms at the median and p95_ms at the 95th."""
    if not posters_ms:
        return "target: no asks with poster timings"
    p50, p95 = percentile(posters_ms, 0.5), percentile(posters_ms, 0.95)
    met = p50 <= p50_ms and p95 <= p95_ms
    return (
        f"target posters p50 <= {p50_ms / 1000:g} s, p95 <= {p95_ms / 1000:g} s: "
        f"p50 {p50 / 1000:.1f} s, p95 {p95 / 1000:.1f} s (n={len(posters_ms)}) -> {'met' if met else 'MISSED'}"
    )


def report(rows: list[dict], last: int, targets: tuple[int, int] = (DEFAULT_P50_MS, DEFAULT_P95_MS)) -> str:
    asks = join(rows)[-last:]
    out = []
    head = f"{'time':8} {'src':5} {'outcome':10} " + " ".join(f"{c:>7}" for c, _, _ in COLUMNS) + "  question"
    out.append(head)
    for a in asks:
        vals = [cell(a, k, w) for _, k, w in COLUMNS]
        out.append(
            f"{a.get('ts', '')[11:19]:8} {str(a.get('source', ''))[:5]:5} {str(a.get('outcome', ''))[:10]:10} "
            + " ".join(f"{'' if v is None else v:>7}" for v in vals)
            + f"  {str(a.get('question', ''))[:50]}"
        )
    meds = []
    for _, k, w in COLUMNS:
        vs = [v for a in asks if (v := cell(a, k, w)) is not None]
        meds.append(round(statistics.median(vs)) if vs else "")
    out.append(f"{'median':8} {'':5} {f'n={len(asks)}':10} " + " ".join(f"{m:>7}" for m in meds))
    out.append(target_line([v for a in asks if (v := cell(a, "posters_ms", "tv")) is not None], *targets))
    if asks:
        first = asks[0].get("ts", "")
        stalls = [r for r in rows if r.get("event") == "loop_stall" and r.get("ts", "") >= first]
        out.append(f"\nserver stalls since {first[11:19]}: {len(stalls)}")
        for s in stalls[-10:]:
            where = s.get("where") or []
            out.append(f"  {s.get('ts', '')[11:19]} {s.get('ms')} ms  at {where[-1] if where else '?'}")
            out.extend(f"      {f}" for f in where[-4:-1])
    return "\n".join(out)


def picks_report(rows: list[dict], last: int) -> str:
    """Per answer shown (show_on_tv calls, TV asks and Claude Desktop alike): the question, Claude's headline and
    subline, and each pick with its service and reason."""
    question = {r["ask_id"]: r.get("question", "") for r in rows if r.get("event") == "ask" and r.get("ask_id")}
    looked = {r.get("turn_id"): r.get("question", "") for r in rows if r.get("tool") == "find_titles"}
    shows = [r for r in rows if r.get("tool") == "show_on_tv" and r.get("status") == "ok" and "picks" in r][-last:]
    out = []
    for r in shows:
        q = question.get(r.get("ask_id")) or looked.get(r.get("turn_id")) or "?"
        src = "tv" if r.get("ask_id") else "desktop"
        out.append(f"{r.get('ts', '')[11:19]} [{src}] “{q}”")
        out.append(f"    {r.get('headline', '')}")
        if r.get("subline"):
            out.append(f"    {r['subline']}")
        for p in r["picks"]:
            out.append(f"      - {p.get('title')} ({p.get('service')}): {p.get('reason') or '(no reason)'}")
    return "\n".join(out) if out else "no answers with picks logged in this period"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", type=Path, default=Path("logs/lumitv.jsonl"))
    p.add_argument("--last", type=int, default=20)
    p.add_argument("--since", default=None, help="ISO time prefix, e.g. 2026-11-04T10:00")
    p.add_argument("--picks", action="store_true", help="what each answer showed and why, instead of timings")
    p.add_argument("--config", type=Path, default=Path("lumitv.toml"), help="its [budgets] set the targets")
    args = p.parse_args()
    rows = read(args.log, args.since)
    print(picks_report(rows, args.last) if args.picks else report(rows, args.last, read_budgets(args.config)))


if __name__ == "__main__":
    main()
