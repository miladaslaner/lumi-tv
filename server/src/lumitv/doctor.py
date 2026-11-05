"""`lumitv doctor`: probe the running server over HTTP and judge it against the configured budgets."""

import shutil
import subprocess
from collections.abc import Callable

import httpx

from . import services as services_mod
from .config import Config
from .tmdb import BASE_URL

PASS, FAIL, INFO = "PASS", "FAIL", "INFO"

# A TV ask that ended like this means Claude Code on this server needs the operator. Doctor can't tell without a real
# `claude -p` run (10-25 s and usage), so it trusts the last ask's outcome for this long (a restart forgets it).
RECENT_ASK_S = 6 * 3600
ASK_PROBLEMS = {
    "login": (
        "Claude Code is logged out: run `claude` and `/login` as the server's user, or set CLAUDE_CODE_OAUTH_TOKEN"
    ),
    "limit": "Claude Code hit its usage limit: asks work again once it resets",
    "not_installed": "Claude Code was not found at [ask] claude_path: install it or fix the path",
}


def _base_url(cfg: Config) -> str:
    host = "127.0.0.1" if cfg.server.host in ("0.0.0.0", "::", "") else cfg.server.host
    return f"http://{host}:{cfg.server.port}"


def _check(ok: bool | None, label: str, detail: str = "") -> tuple[str, bool | None]:
    tag = INFO if ok is None else PASS if ok else FAIL
    return f"{tag:4}  {label}" + (f" ({detail})" if detail else ""), ok


def _budget(label: str, value: float | None, limit: float, what: str) -> tuple[str, bool | None]:
    if value is None:
        return _check(None, label, "no data yet")
    return _check(value <= limit, label, f"{what} {value:g} ms, budget {limit:g} ms")


def claude_version(path: str) -> str | None:
    """`claude --version` of the Claude Code CLI asks use; None when it is missing or broken."""
    exe = shutil.which(path) or path  # Windows: PATHEXT
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out.splitlines()[0] if r.returncode == 0 and out else None


def _last_ask(last: dict) -> tuple[str, bool | None]:
    """How the last ask from the TV ended: FAIL when it was recent and Claude Code needs the operator."""
    outcome, age = last.get("outcome"), last.get("age_s")
    ago = f"{round(age / 60)} min ago" if isinstance(age, (int, float)) else "time unknown"
    problem = ASK_PROBLEMS.get(outcome) if isinstance(outcome, str) else None
    if problem and isinstance(age, (int, float)) and age <= RECENT_ASK_S:
        return _check(False, "Last ask from the TV", f"{outcome}, {ago}: {problem}")
    return _check(None, "Last ask from the TV", f"{outcome}, {ago}")


def contact_check(cfg: Config) -> tuple[str, bool | None]:
    """First-time setup: services that open exact titles need ids from Wikidata, which refuses a client that names
    no contact ([open] user_agent_contact). None when no service needs those ids. Never echoes the contact."""
    if not any(s.wanted_ids for s in services_mod.load(cfg).values()):
        return _check(None, "Wikidata contact", "no service needs title ids")
    if cfg.open.user_agent_contact.strip():
        return _check(True, "Wikidata contact", "set")
    return _check(
        False, "Wikidata contact",
        "set [open] user_agent_contact (an email or URL) in lumitv.toml: without it Wikidata refuses lookups and "
        "picks open the app's home screen instead of the title",
    )  # fmt: skip


def run_checks(
    cfg: Config, client: httpx.Client, claude_version: Callable[[str], str | None] = claude_version
) -> tuple[list[str], bool]:
    """Returns (report lines, all_ok). Never includes token values."""
    results: list[tuple[str, bool | None]] = [contact_check(cfg)]

    if cfg.ask.enabled:
        version = claude_version(cfg.ask.claude_path)
        if version:
            results.append(_check(None, "Claude Code", version))
        else:
            results.append(
                _check(False, "Claude Code", f"{cfg.ask.claude_path!r} not found or not working; asks from the TV fail")
            )

    try:
        r = client.get(
            f"{BASE_URL}/authentication",
            headers={"Authorization": f"Bearer {cfg.tmdb_token}"},
            timeout=cfg.timeouts.tmdb_s,
        )
        results.append(_check(r.status_code == 200, "TMDB token valid", f"HTTP {r.status_code}"))
    except httpx.HTTPError as e:
        results.append(_check(False, "TMDB token valid", type(e).__name__))

    base = _base_url(cfg)
    stats = None
    try:
        health = client.get(f"{base}/health", timeout=3)
        results.append(_check(health.status_code == 200, "Server up", base))
        r = client.get(f"{base}/stats", headers={"Authorization": f"Bearer {cfg.server_token}"}, timeout=3)
        if r.status_code == 200:
            try:
                stats = r.json()
                if not isinstance(stats, dict) or not all(
                    isinstance(stats.get(k), dict) for k in ("find", "show", "render")
                ):
                    raise ValueError("missing keys")
            except ValueError:
                stats = None
                results.append(_check(False, "/stats readable", "unexpected response"))
        else:
            results.append(_check(False, "/stats reachable", f"HTTP {r.status_code}"))
    except httpx.HTTPError as e:
        results.append(_check(False, "Server up", f"{base}: {type(e).__name__}"))

    if stats:
        tv = stats.get("tv") or {}
        if tv.get("connected"):
            state = ", ".join(f"{k}={'yes' if tv.get(k) else 'no'}" for k in ("screen_on", "playing", "vpn"))
            if tv.get("playing_known") is False:
                state += "; playback state unknown (answers arrive as a notice): re-allow the notification listener"
            results.append(_check(True, "TV connected", state))
        else:
            results.append(_check(False, "TV connected", "not connected"))
        rows = stats.get("cache_rows")
        if rows is not None:
            results.append(_check(rows > 0, "TMDB cache populated", f"{rows} rows"))
        hit = stats.get("cache_hit_rate")
        results.append(
            _check(
                None, "Stats", f"{stats.get('turns', 0)} turns, cache hit rate {'n/a' if hit is None else f'{hit:.0%}'}"
            )
        )
        b = cfg.budgets
        find, show, render = stats["find"], stats["show"], stats["render"]
        for part in (find, show, render):
            part.setdefault("p95", None)
        results.append(_budget("find_titles p95 (cold budget)", find["p95"], b.find_p95_cold_ms, "p95"))
        if hit is not None and hit >= 0.9:
            results.append(_budget("find_titles p95 (warm budget)", find["p95"], b.find_p95_warm_ms, "p95"))
        results.append(_budget("show_on_tv to TV rendered p95", render["p95"], b.render_ms, "p95"))
        if find["p95"] is not None and show["p95"] is not None:
            results.append(
                _budget(
                    "server time per turn (find + show p95)", find["p95"] + show["p95"], b.followup_server_ms, "sum"
                )
            )
        else:
            results.append(_check(None, "server time per turn (find + show p95)", "no data yet"))
        results.append(
            _check(
                None,
                f"looking < {b.looking_ms} ms, posters p50 < {b.posters_p50_ms} ms",
                "end to end, measured on the TV",
            )
        )
        ask = stats.get("ask")
        if cfg.ask.enabled and isinstance(ask, dict) and isinstance(ask.get("last"), dict):
            results.append(_last_ask(ask["last"]))
        if isinstance(ask, dict):  # asks from the TV, measured on the TV
            for stage, label, limit in (
                ("listen", "ask: press to listening on screen p50", b.listen_ms),
                ("heard", "ask: end of speech to Looking p50", b.heard_ms),
                ("posters", "ask: end of speech to posters p50", b.ask_posters_p50_ms),
            ):
                part = ask.get(stage) if isinstance(ask.get(stage), dict) else {}
                results.append(_budget(label, part.get("p50"), limit, "p50"))

    lines = [text for text, _ in results]
    ok = all(v is not False for _, v in results)
    lines.append("Overall: " + ("PASS" if ok else "FAIL"))
    return lines, ok


def run(cfg: Config) -> int:
    with httpx.Client() as client:
        lines, ok = run_checks(cfg, client)
    print("\n".join(lines))
    return 0 if ok else 1
