"""End-to-end check without a TV: a throwaway server, the fake TV and an MCP client playing Claude's turns.

    uv run python scripts/e2e_check.py [--host <address>] [--via-loopback]        (from lumi-tv/server)

What it does:
- Starts its own server on a free port: your lumitv.toml and .env, but a copy of your database (so the cache is
  warm and what the run writes stays out of yours), a temporary log, asks off and no speech-to-text. A server you
  already run, its database, its log and its port are not touched; the copy is deleted afterwards.
- Starts scripts/fake_tv.py against it and plays six turns through the MCP client against real TMDB: a binge
  question, a lighter follow-up, opening a pick, kids mode for a three-year-old, an answer while something plays
  (notice) and the TV gone (offline). Prints each turn's picks, a timing table and GET /stats.
- Does not cover asks from the TV (POST /ask, /ask/audio): those need Claude Code and a subscription.

The clients connect to this machine's LAN address, so [server] host must be 0.0.0.0 (the example is). --host sets
the address when the detected one is wrong (a VPN is up, several interfaces); --via-loopback connects to 127.0.0.1
while sending the LAN address as Host (a firewall that blocks the LAN path). Needs TMDB_TOKEN and SERVER_TOKEN in
.env; the token is never printed. Exit code 0 when every check held, 1 otherwise.
"""

import argparse
import asyncio
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from lumitv import config

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "lumitv.toml"

# Runs in the server subprocess: the real config with a free port, the copied database, a temporary log, asks off
# and no speech-to-text. The CLI only reads a toml as is, and Python cannot write one, so the overrides go in here.
SERVER_MAIN = """
import sys
from pathlib import Path
from lumitv import config
from lumitv.app import LumiServer, create_app, make_uvicorn_config
toml, port, db, log = sys.argv[1:5]
cfg = config.load(toml)
cfg = cfg.model_copy(update={
    "server": cfg.server.model_copy(update={"port": int(port), "db_path": Path(db), "log_path": Path(log)}),
    "ask": cfg.ask.model_copy(update={"enabled": False}),
    "speech": cfg.speech.model_copy(update={"engine": "none"}),
})
LumiServer(make_uvicorn_config(create_app(cfg), cfg)).run()
"""


class E2EFailure(Exception):
    """A check that did not hold; the message says which."""


def check(ok, what: str) -> None:
    if not ok:
        raise E2EFailure(what)


def lan_ip() -> str:
    """This machine's address on the LAN: the source address of a UDP socket pointed at the LAN (nothing is sent).
    Without a route (no network) macOS can still name its Wi-Fi or Ethernet address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        pass
    if sys.platform == "darwin":
        for iface in ("en0", "en1"):
            got = subprocess.run(["ipconfig", "getifaddr", iface], capture_output=True, text=True, check=False)
            if got.stdout.strip():
                return got.stdout.strip()
    raise E2EFailure("could not find this machine's LAN address; pass --host")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def copy_database(src: Path, dst: Path) -> bool:
    """A consistent copy of the server's SQLite database (the backup API, so a running server is no problem).
    False when there is none yet; the throwaway server then starts with an empty cache and warms it."""
    if not src.exists():
        return False
    a, b = sqlite3.connect(src), sqlite3.connect(dst)
    try:
        a.backup(b)
    finally:
        b.close()
        a.close()
    return True


def stop(proc: subprocess.Popen, grace: float) -> None:
    """Ask a child to stop (Ctrl-C on POSIX; Windows has no signal for that), then kill it."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        proc.terminate()
    else:
        proc.send_signal(signal.SIGINT)
    try:
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        proc.kill()


class FakeTv:
    def __init__(self, host: str, port: int, token: str, *flags: str):
        self.lines: list[tuple[float, str]] = []
        env = {**os.environ, "SERVER_TOKEN": token}
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "scripts" / "fake_tv.py"), "--url", f"ws://{host}:{port}/tv", *flags],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, cwd=ROOT,
        )  # fmt: skip
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.append((time.time(), line.rstrip()))

    def mark(self) -> int:
        return len(self.lines)

    def since(self, mark: int) -> list[str]:
        return [ln.split(" ", 2)[1] for _, ln in self.lines[mark:]]

    def find(self, mark: int, kind: str) -> list[str]:
        return [ln for _, ln in self.lines[mark:] if ln.split(" ", 2)[1] == kind]

    def stop(self):
        stop(self.proc, 3)


def payload(result) -> dict:
    check(not result.is_error, f"tool error: {result.content}")
    return json.loads(result.content[0].text)


async def wait_for(pred, what: str, timeout: float = 15.0):
    end = time.time() + timeout
    while time.time() < end:
        v = await pred()
        if v:
            return v
        await asyncio.sleep(0.1)
    raise E2EFailure(f"timed out waiting for {what}")


async def main(host: str, loopback: bool) -> int:
    cfg = config.load(CONFIG)  # the real config and .env; the server below runs on a copy of its data
    token = cfg.server_token
    port = free_port()
    # --via-loopback: connect to 127.0.0.1 but present the LAN address as Host (host-check coverage only)
    addr = "127.0.0.1" if loopback else host
    base = f"http://{addr}:{port}"
    auth = {"Authorization": f"Bearer {token}"}
    if loopback:
        auth["Host"] = f"{host}:{port}"
    if cfg.server.host != "0.0.0.0" and not loopback:
        print(f"note: [server] host is {cfg.server.host}; pass --host {cfg.server.host} if the clients cannot connect")
    with tempfile.TemporaryDirectory(prefix="lumi-e2e-") as tmp:
        work = Path(tmp)
        db = work / "lumitv.db"
        if copy_database(ROOT / cfg.server.db_path, db):
            print(f"server: port {port}, a copy of {cfg.server.db_path}, temporary log")
        else:
            print(f"server: port {port}, no {cfg.server.db_path} to copy (it warms an empty cache in the background)")
        server_out = (work / "server.log").open("w", encoding="utf-8")
        server = subprocess.Popen(
            [sys.executable, "-c", SERVER_MAIN, str(CONFIG), str(port), str(db), str(work / "lumitv.jsonl")],
            cwd=ROOT, stdout=server_out, stderr=subprocess.STDOUT,
        )  # fmt: skip
        tv: FakeTv | None = None
        rows: list[tuple] = []
        try:
            async with httpx2.AsyncClient(headers=auth, timeout=30) as http:

                async def healthy():
                    try:
                        return (await http.get(f"{base}/health")).text == "ok"
                    except httpx2.HTTPError:
                        return False

                await wait_for(healthy, "server /health")
                print(f"server up; client Host = {auth.get('Host', f'{host}:{port}')}, connecting to {addr}")

                async def tv_state(playing: bool):
                    async def pred():
                        st = (await http.get(f"{base}/stats")).json()["tv"]
                        return st["connected"] and st["playing"] == playing

                    await wait_for(pred, f"fake TV connected (playing={playing})")

                tv = FakeTv(addr, port, token, "--screen-on", "--not-playing")
                await tv_state(False)

                async with streamable_http_client(f"{base}/mcp", http_client=http) as streams:
                    async with ClientSession(*streams[:2]) as s:
                        await s.initialize()
                        tools = sorted(t.name for t in (await s.list_tools()).tools)
                        print("tools:", ", ".join(tools))

                        async def turn(name, tv, question, headline, find_args, expect, n=3, tokens=None):
                            mark = tv.mark()
                            t0 = time.perf_counter()
                            found = payload(await s.call_tool("find_titles", {"question": question, **find_args}))
                            find_ms = (time.perf_counter() - t0) * 1000
                            titles = found["titles"]
                            # no silent widening: the question as asked must work
                            check(len(titles) >= n, f"{name}: only {len(titles)} titles for {find_args}")
                            picks = tokens(titles) if tokens else titles[:n]
                            t1 = time.perf_counter()
                            shown = payload(
                                await s.call_tool(
                                    "show_on_tv",
                                    {"headline": headline, "subline": "e2e", "tokens": [p["token"] for p in picks]},
                                )
                            )
                            show_ms = (time.perf_counter() - t1) * 1000
                            check(shown["status"] == expect, f"{name}: show returned {shown['status']}, want {expect}")
                            await asyncio.sleep(0.3)
                            seen = tv.since(mark)
                            if expect in ("screen", "notice"):
                                if expect == "screen":
                                    check("looking" in seen, f"{name}: no looking")
                                    check(seen.index("looking") < seen.index("show"), f"{name}: looking after show")
                                else:
                                    check("looking" not in seen, f"{name}: looking sent while playing")
                                render = tv.find(mark, "rendered_sent")
                                render_ms = render[0].split("ms=")[1] if render else "-"
                            else:
                                render_ms = "-"
                            rows.append((name, f"{find_ms:.0f}", f"{show_ms:.0f}", render_ms, expect))
                            certs = [t["certification"] for t in picks]
                            print(f"{name}: {[t['title'] for t in picks]} certs={certs} -> {shown['status']}")
                            return titles, picks

                        # 1 binge question, as the spec asks it
                        q1 = "We liked Matrix, Lord of the Rings, Star Trek. What to binge next for 10-15 hours?"
                        f1 = {
                            "like": ["The Matrix", "The Lord of the Rings", "Star Trek"], "kind": "tv",
                            "min_hours": 10, "max_hours": 15,
                        }  # fmt: skip
                        binge, first = await turn(
                            "1 binge", tv, q1, "Three bingeable epics. Want something lighter?", f1, "screen"
                        )
                        for t in binge:
                            label = t["length"] or ""
                            hours = int(label.split("≈")[1].strip(" h")) if "≈" in label else None
                            check(t.get("binge_minutes") is not None, f"1 binge: {t['title']} has no matched span")
                            check(label.startswith(("≈", "S1")), f"1 binge: bad label {label!r}")
                            check(t["kind"] == "tv", f"1 binge: {t['title']} is a {t['kind']}")
                            in_range = hours is not None and 10 <= hours <= 15
                            check(in_range, f"1 binge: {t['title']} is {label}, not 10-15 h")
                        print(f"1 binge: {[(t['title'], t['length']) for t in binge]}")
                        first_ids = [t["tmdb_id"] for t in first]

                        # 2 follow-up excluding the first picks
                        _, second = await turn(
                            "2 lighter", tv, "something lighter", "Lighter picks. Any of these?",
                            {"genres": ["comedy"], "kind": "tv", "exclude_tmdb_ids": first_ids}, "screen",
                        )  # fmt: skip
                        check(not {t["tmdb_id"] for t in second} & set(first_ids), "follow-up repeated a title")

                        # 3 open one
                        mark = tv.mark()
                        t0 = time.perf_counter()
                        opened = payload(await s.call_tool("open_title", {"token": second[1]["token"]}))
                        open_ms = (time.perf_counter() - t0) * 1000
                        await asyncio.sleep(0.3)
                        # a wrong VPN state only adds a vpn_warning; the open is still sent
                        check(opened["status"] == "sent", f"3 open: {opened}")
                        check("open" in tv.since(mark), "open not delivered")
                        rows.append(("3 open", "-", f"{open_ms:.0f}", "-", opened["status"]))
                        print("3 open:", second[1]["title"], "->", opened["status"])

                        # 4 kids mode, age 3
                        _, kids = await turn(
                            "4 kids 3y", tv, "calm shows for my 3-year-old", "Gentle picks for a little one. Which?",
                            {"kids_mode": True, "kids_age": 3, "genres": ["animation"]}, "screen",
                        )  # fmt: skip
                        check(all(t["certification"] == "AL" for t in kids), "kids pick not AL")

                        # 5 playing -> notice
                        tv.stop()
                        await asyncio.sleep(0.5)
                        tv = FakeTv(addr, port, token, "--screen-on", "--playing")
                        await tv_state(True)
                        _, playing = await turn(
                            "5 playing", tv, "something funny", "Something funny. Any good?", {"genres": ["comedy"]},
                            "notice",
                        )  # fmt: skip

                        # 6 TV gone -> offline fast
                        tv.stop()

                        async def tv_gone():
                            return not (await http.get(f"{base}/stats")).json()["tv"]["connected"]

                        await wait_for(tv_gone, "server to see the fake TV disconnect")
                        t1 = time.perf_counter()
                        gone = payload(
                            await s.call_tool(
                                "show_on_tv",
                                {"headline": "Still there?", "subline": "e2e", "tokens": [p["token"] for p in playing]},
                            )
                        )
                        off_ms = (time.perf_counter() - t1) * 1000
                        quickly = gone["status"] == "offline" and off_ms < 5000
                        check(quickly, f"6 offline: {gone['status']} in {off_ms:.0f} ms")
                        rows.append(("6 offline", "-", f"{off_ms:.0f}", "-", "offline"))
                        print(f"6 offline: {gone['status']} in {off_ms:.0f} ms")

                stats = (await http.get(f"{base}/stats")).json()

            print("\nturn        find_ms  show_ms  render_ms  status")
            for r in rows:
                print(f"{r[0]:<11} {r[1]:>7}  {r[2]:>7}  {r[3]:>9}  {r[4]}")
            print("\nGET /stats:")
            print(json.dumps(stats, indent=2))
            print("\nE2E OK")
            return 0
        except (E2EFailure, BaseExceptionGroup) as e:
            print(f"E2E FAILED: {e!r}", file=sys.stderr)
            server_out.flush()
            tail = (work / "server.log").read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
            if tail:
                print("server output (last lines):\n  " + "\n  ".join(tail), file=sys.stderr)
            return 1
        finally:
            if tv:
                tv.stop()
            stop(server, 5)
            server_out.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=None, help="the address the clients use (default: this machine's LAN address)")
    ap.add_argument(
        "--via-loopback", action="store_true",
        help="connect to 127.0.0.1 but send the LAN address as Host (when a firewall blocks the LAN path)",
    )  # fmt: skip
    a = ap.parse_args()
    try:
        sys.exit(asyncio.run(main(a.host or lan_ip(), a.via_loopback)))
    except E2EFailure as e:
        sys.exit(f"E2E FAILED: {e}")
