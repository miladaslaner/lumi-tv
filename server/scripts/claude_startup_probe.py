"""Where does Claude Code's start-up time go? Run on the server PC (logged-in Claude Code; stdlib only).

    uv run python scripts/claude_startup_probe.py                 # every variant, 2 runs each
    uv run python scripts/claude_startup_probe.py --only stream-prewarmed --idle 40

Each variant runs `claude` in a fresh empty working directory (like the server's claude-home) with the env the server
sets, and asks a one-word question (a few small requests on the subscription; avoid 00:00-06:00 on a shared PC):

- oneshot-*      `claude -p --output-format json` per question. start-up = wall time - the CLI's own duration_ms.
                 default: no extra flags; local-settings: + `--setting-sources local` (skips the user's settings,
                 hooks, plugins); lumi-flags: + the server's flags (`--strict-mcp-config` with no servers,
                 `--tools ""`).
- oneshot-debug  lumi-flags with `--debug-file`: prints the largest gaps between debug lines, i.e. the slow phases.
- stream-cold    the server's long-lived stream-json process, question sent right after spawn: turn 1 pays the
                 start-up, turn 2 shows a warm turn.
- stream-prewarmed  the same, but turn 1 is sent `--idle` seconds after spawn (what the server's pre-warmed spare
                 does). If turn 1 here is close to turn 2, pre-warming hides the start-up; if it is close to
                 stream-cold's turn 1, the CLI defers its start-up work to the first question.
Nothing here prints tokens or the environment.
"""

import argparse
import itertools
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PROMPT = "Reply with the single word ok"
QUIET_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)Z?\s")
SECRETISH = re.compile(r"(sk-ant-[\w-]+|Bearer\s+\S+|token[\"'=:\s]+\S+)", re.IGNORECASE)


def base_args(model: str) -> list[str]:
    return ["-p", "--model", model]


def lumi_flags(workdir: Path) -> list[str]:
    empty = workdir / "mcp-empty.json"
    empty.write_text('{"mcpServers": {}}', encoding="utf-8")
    return ["--setting-sources", "local", "--mcp-config", str(empty), "--strict-mcp-config", "--tools", ""]


def oneshot(claude: str, model: str, extra: list[str], workdir: Path, timeout: float) -> dict:
    args = [claude, *base_args(model), "--output-format", "json", *extra]
    t0 = time.monotonic()
    p = subprocess.run(args, input=PROMPT.encode(), capture_output=True, cwd=workdir, timeout=timeout, check=False,
                       env={**os.environ, **QUIET_ENV})  # fmt: skip
    wall = (time.monotonic() - t0) * 1000
    try:
        out = json.loads(p.stdout.decode("utf-8", "replace"))
    except ValueError:
        return {"wall_ms": wall, "error": f"exit {p.returncode}: {p.stderr.decode('utf-8', 'replace')[:200]}"}
    return {
        "wall_ms": wall,
        "cli_ms": out.get("duration_ms"),
        "api_ms": out.get("duration_api_ms"),
        "startup_ms": wall - (out.get("duration_ms") or 0),
        "is_error": out.get("is_error"),
    }


def debug_gaps(path: Path, top: int) -> list[str]:
    stamps = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = STAMP.match(line)
        if m:
            h, mi, s = int(m.group(2)), int(m.group(3)), float(m.group(4))
            stamps.append((h * 3600 + mi * 60 + s, line[m.end() :]))
    if len(stamps) < 2:
        return ["(no timestamped debug lines)"]
    first = stamps[0][0]
    gaps = sorted(((b[0] - a[0], a[0] - first, a[1], b[1]) for a, b in itertools.pairwise(stamps)), reverse=True)
    lines = [f"debug log spans {stamps[-1][0] - first:.2f} s over {len(stamps)} lines; largest gaps:"]
    for gap, at, before, after in gaps[:top]:
        lines.append(f"  {gap * 1000:7.0f} ms at +{at:6.2f} s  after: {_clean(before)}")
        lines.append(f"  {'':7}    {'':10}  before: {_clean(after)}")
    return lines


def _clean(text: str) -> str:
    return SECRETISH.sub("[redacted]", text)[:150]


class Stream:
    """The server's long-lived stream-json process."""

    def __init__(self, claude: str, model: str, workdir: Path):
        args = [claude, *base_args(model), "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", *lumi_flags(workdir)]  # fmt: skip
        self.t0 = time.monotonic()
        self.p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  cwd=workdir, env={**os.environ, **QUIET_ENV})  # fmt: skip
        self.first_line_ms: float | None = None
        self.results: list[tuple[float, dict]] = []
        self.inits: list[float] = []
        self.cond = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=lambda: self.p.stderr.read(), daemon=True).start()

    def _read(self) -> None:
        for raw in self.p.stdout:
            now = time.monotonic()
            if self.first_line_ms is None:
                self.first_line_ms = (now - self.t0) * 1000
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            with self.cond:
                if msg.get("type") == "system" and msg.get("subtype") == "init":
                    self.inits.append(now)
                if msg.get("type") == "result":
                    self.results.append((now, msg))
                self.cond.notify_all()
        with self.cond:
            self.cond.notify_all()

    def turn(self, timeout: float) -> dict:
        n = len(self.results)
        line = {"type": "user", "message": {"role": "user", "content": PROMPT}, "parent_tool_use_id": None,
                "session_id": ""}  # fmt: skip
        t = time.monotonic()
        self.p.stdin.write((json.dumps(line) + "\n").encode("ascii"))
        self.p.stdin.flush()
        with self.cond:
            if not self.cond.wait_for(lambda: len(self.results) > n or self.p.poll() is not None, timeout):
                return {"error": "timeout"}
            if len(self.results) <= n:
                return {"error": f"process exited {self.p.returncode}"}
            done, msg = self.results[n]
            init = next((i for i in self.inits if i >= t), None)
        return {
            "turn_ms": (done - t) * 1000,
            "to_init_ms": (init - t) * 1000 if init else None,
            "cli_ms": msg.get("duration_ms"),
            "api_ms": msg.get("duration_api_ms"),
            "is_error": msg.get("is_error"),
        }

    def close(self) -> None:
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.p.kill()


def stream(claude: str, model: str, workdir: Path, idle: float, timeout: float) -> dict:
    s = Stream(claude, model, workdir)
    try:
        time.sleep(idle)
        t1 = s.turn(timeout)
        t2 = s.turn(timeout)
        return {"spawn_to_first_line_ms": s.first_line_ms, "idle_s": idle, "turn1": t1, "turn2": t2}
    finally:
        s.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--claude", default="claude", help="the Claude Code CLI (name on PATH or full path)")
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--idle", type=float, default=30.0, help="stream-prewarmed: seconds between spawn and turn 1")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--top", type=int, default=12, help="oneshot-debug: how many gaps to print")
    ap.add_argument("--only", nargs="*", help="variant names to run")
    ap.add_argument("--json", action="store_true", help="also print every result as JSON")
    a = ap.parse_args()
    claude = shutil.which(a.claude) or a.claude
    if not Path(claude).exists() and shutil.which(claude) is None:
        sys.exit(f"Claude Code CLI not found: {a.claude}")
    version = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=60, check=False).stdout
    version = version.strip()
    print(f"claude: {claude} ({version}); model {a.model}; {a.runs} run(s) per variant")

    variants = {
        "oneshot-default": lambda wd: oneshot(claude, a.model, [], wd, a.timeout),
        "oneshot-local-settings": lambda wd: oneshot(claude, a.model, ["--setting-sources", "local"], wd, a.timeout),
        "oneshot-lumi-flags": lambda wd: oneshot(claude, a.model, lumi_flags(wd), wd, a.timeout),
        "oneshot-debug": None,
        "stream-cold": lambda wd: stream(claude, a.model, wd, 0.0, a.timeout),
        "stream-prewarmed": lambda wd: stream(claude, a.model, wd, a.idle, a.timeout),
    }
    for name in a.only or variants:
        if name not in variants:
            sys.exit(f"unknown variant {name}; choose from {', '.join(variants)}")
        results = []
        for _ in range(a.runs if name != "oneshot-debug" else 1):
            with tempfile.TemporaryDirectory(prefix="claude-probe-") as d:
                wd = Path(d)
                if name == "oneshot-debug":
                    dbg = wd / "debug.txt"
                    r = oneshot(claude, a.model, [*lumi_flags(wd), "--debug-file", str(dbg)], wd, a.timeout)
                    r["gaps"] = debug_gaps(dbg, a.top) if dbg.exists() else ["(no debug file written)"]
                else:
                    r = variants[name](wd)
            results.append(r)
        report(name, results, a.json)


def report(name: str, results: list[dict], as_json: bool) -> None:
    print(f"\n== {name}")
    for r in results:
        if "gaps" in r:
            print("\n".join(r["gaps"]))
        if as_json:
            print(json.dumps({k: v for k, v in r.items() if k != "gaps"}))

    def med(key, sub=None):
        vals = [(r.get(sub) or {}).get(key) if sub else r.get(key) for r in results]
        vals = [v for v in vals if isinstance(v, int | float)]
        return f"{statistics.median(vals):8.0f}" if vals else "       -"

    errors = [r.get("error") or (r.get("turn1") or {}).get("error") for r in results]
    if any(errors):
        print("errors:", [e for e in errors if e])
    if name.startswith("oneshot"):
        print(f"median ms: wall {med('wall_ms')}  cli {med('cli_ms')}  api {med('api_ms')}  "
              f"start-up {med('startup_ms')}")  # fmt: skip
    else:
        print(f"median ms: spawn->first output {med('spawn_to_first_line_ms')}  (none before turn 1 unless hooks run)")
        print(f"  turn 1: total {med('turn_ms', 'turn1')}  ->init {med('to_init_ms', 'turn1')}  "
              f"cli {med('cli_ms', 'turn1')}  api {med('api_ms', 'turn1')}")  # fmt: skip
        print(f"  turn 2: total {med('turn_ms', 'turn2')}  ->init {med('to_init_ms', 'turn2')}  "
              f"cli {med('cli_ms', 'turn2')}  api {med('api_ms', 'turn2')}")  # fmt: skip


if __name__ == "__main__":
    main()
