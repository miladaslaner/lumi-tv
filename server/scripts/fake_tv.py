"""Fake Lumi TV: connects to /tv, reports state, prints every message, acks `shown`.

Usage: uv run python scripts/fake_tv.py --screen-on --not-playing [--vpn]
       uv run python scripts/fake_tv.py --playing-unknown      # a TV without the notification-listener grant
       uv run python scripts/fake_tv.py --ask "something funny" [--answer-id A --focused-token T]
  --ask: once connected, asks like the TV's keyboard does (POST /ask, source "typed", a random ask_id); each message
  is then printed with the ms since the POST.
URL and token come from --url/--token or LUMITV_URL / SERVER_TOKEN (read from .env next to lumitv.toml).
The token is never printed.
"""

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from dotenv import dotenv_values

from lumitv.tvlink import CLOSE_BAD_TOKEN

ASKED_AT: list[float] = []  # perf_counter() of the POST /ask, when --ask


def out(kind: str, detail: str = "") -> None:
    since = f" +{(time.perf_counter() - ASKED_AT[0]) * 1000:.0f}ms" if ASKED_AT else ""
    print(f"{time.time():.3f}{since} {kind} {detail}".rstrip(), flush=True)


def ask_url(ws_url: str) -> str:
    """ws://host:port/tv -> http://host:port/ask"""
    parts = urlsplit(ws_url)
    return urlunsplit(("https" if parts.scheme == "wss" else "http", parts.netloc, "/ask", "", ""))


async def post_ask(url: str, token: str, text: str, answer_id: str | None, focused_token: str | None) -> None:
    body = {"ask_id": secrets.token_hex(8), "text": text, "source": "typed"}
    if answer_id:
        body["answer_id"] = answer_id
    if focused_token:
        body["focused_token"] = focused_token
    ASKED_AT.append(time.perf_counter())
    out("ask", f"ask_id={body['ask_id']} text={text!r}")
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
    out("ask_posted", f"HTTP {r.status_code} {r.text}")


def state_message(screen_on: bool, playing: bool, vpn: bool, playing_known: bool = True) -> dict:
    """The TV's `state` (protocol.md). playing_known false means the TV cannot read playback state (no
    notification-listener grant); the app then reports playing, so the server never covers a video."""
    return {
        "type": "state", "screen_on": screen_on, "playing": playing or not playing_known, "vpn": vpn,
        "playing_known": playing_known,
    }  # fmt: skip


def summarize(msg: dict) -> str:
    if msg.get("type") == "show":
        # with each pick's token, for --focused-token
        titles = ", ".join(f"{p['title']} ({p.get('token', '?')})" for p in msg["picks"])
        return f"answer_id={msg['answer_id']} headline={msg['headline']!r} picks=[{titles}]"
    if msg.get("type") == "hello":
        return f"assistant={msg.get('assistant_name')}"
    return json.dumps({k: v for k, v in msg.items() if k != "token"})


async def run(
    url: str, token: str, screen_on: bool, playing: bool, vpn: bool, ask: dict | None = None, playing_known: bool = True
) -> None:
    async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {token}"}) as ws:
        state = state_message(screen_on, playing, vpn, playing_known)
        playing = state["playing"]
        out("connected", f"screen_on={screen_on} playing={playing} playing_known={playing_known} vpn={vpn}")
        await ws.send(json.dumps(state))
        if ask:
            await post_ask(ask_url(url), token, **ask)
        async for raw in ws:
            received = time.perf_counter()
            msg = json.loads(raw)
            out(msg.get("type", "?"), summarize(msg))
            if msg.get("type") == "show":
                mode = "notice" if playing else "screen"
                await ws.send(json.dumps({"type": "shown", "answer_id": msg["answer_id"], "mode": mode}))
                out("acked", mode)
                ms = round((time.perf_counter() - received) * 1000)
                await ws.send(json.dumps({"type": "rendered", "answer_id": msg["answer_id"], "ms": ms}))
                out("rendered_sent", f"ms={ms}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=os.environ.get("LUMITV_URL", "ws://127.0.0.1:8765/tv"))
    p.add_argument("--token", default=None)
    on = p.add_mutually_exclusive_group()
    on.add_argument("--screen-on", dest="screen_on", action="store_true", default=True)
    on.add_argument("--screen-off", dest="screen_on", action="store_false")
    pl = p.add_mutually_exclusive_group()
    pl.add_argument("--playing", dest="playing", action="store_true", default=False)
    pl.add_argument("--not-playing", dest="playing", action="store_false")
    vp = p.add_mutually_exclusive_group()
    vp.add_argument("--vpn", dest="vpn", action="store_true", default=False)
    vp.add_argument("--no-vpn", dest="vpn", action="store_false")
    p.add_argument(
        "--playing-unknown", action="store_true",
        help="like a TV without the notification-listener grant: playing_known false (and so playing true)",
    )  # fmt: skip
    p.add_argument("--ask", default=None, help="ask this (typed) once connected")
    p.add_argument("--answer-id", default=None, help="with --ask: the answer on screen")
    p.add_argument("--focused-token", default=None, help="with --ask: its focused poster")
    args = p.parse_args()
    env_file = Path(__file__).parents[1] / ".env"
    token = args.token or os.environ.get("SERVER_TOKEN") or dotenv_values(env_file).get("SERVER_TOKEN")
    if not token:
        print("No token: pass --token, set SERVER_TOKEN, or add it to .env", file=sys.stderr)
        return 2
    try:
        ask = None
        if args.ask is not None:
            ask = {"text": args.ask, "answer_id": args.answer_id, "focused_token": args.focused_token}
        asyncio.run(run(args.url, token, args.screen_on, args.playing, args.vpn, ask, not args.playing_unknown))
    except KeyboardInterrupt:
        pass
    except OSError as e:  # refused, unreachable, timed out
        print(f"Cannot connect to {args.url}: {e}", file=sys.stderr)
        return 1
    except websockets.InvalidHandshake as e:
        print(f"Server rejected the connection to {args.url}: {e}", file=sys.stderr)
        return 1
    except websockets.ConnectionClosed as e:
        code = e.rcvd.code if e.rcvd else None
        out("closed", f"code={code}")
        if code == CLOSE_BAD_TOKEN:
            print("Server rejected the token (close 4401); check SERVER_TOKEN", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
