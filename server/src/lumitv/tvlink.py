"""WebSocket hub between the server and the Android TV app. Wire format: lumi-tv/protocol.md."""

import asyncio
import hmac
import json
import logging
import secrets
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketDisconnected

from .catalog import Pick
from .config import Config, Open
from .services import DEFAULT_OPEN, Service, launch_chain, launch_steps, provider_order

log = logging.getLogger("lumitv.tvlink")

CONTENT_DIR = Path(__file__).resolve().parents[2] / "content"
CLOSE_BAD_TOKEN = 4401
CLOSE_REPLACED = 4000
CLOSE_UNSUPPORTED_DATA = 1003  # RFC 6455: a binary frame on a text protocol
# the socket went away under a send: Starlette's own disconnect (1006 on a failed send, or a send after the close),
# or the transport's
_SOCKET_GONE = (WebSocketDisconnect, WebSocketDisconnected, OSError)

ShowResult = Literal["screen", "notice", "tv_off", "offline"]
OpenResult = Literal["sent", "tv_off", "offline"]
SHOWN_MODES = ("screen", "notice", "tv_off")
AskErrorCode = Literal["not_heard", "failed", "timeout", "login", "limit", "not_installed"]
ANSWERS_KEPT = 20  # shown answers answer_context can describe
NBSP = " "  # inside a card's length label: "≈ 13h" must never wrap as "≈" / "13h" (Claude's `length` stays plain)
# ask_timing fields (TV clock) -> metrics.AskStage
RENDER_PHASES = ("start", "created", "frame")  # `rendered` phases: activity started, created, first frame drawn
ASK_TIMING_FIELDS = {
    "listen_ms": "listen",
    "heard_ms": "heard",
    "posters_ms": "posters",
    "typed_posters_ms": "typed_posters",
}


@dataclass
class TvState:
    connected: bool = False
    screen_on: bool = True
    playing: bool = False
    vpn: bool = False
    playing_known: bool = True  # false: the TV can't read playback state, so it may be playing

    @property
    def maybe_playing(self) -> bool:
        return self.playing or not self.playing_known


@dataclass
class ScreenContext:
    """What an answer put on the TV, to seed a follow-up ask: titles are (title, kind, tmdb_id)."""

    headline: str
    titles: list[tuple[str, str, int]]
    focused: str | None


@dataclass
class _Shown:
    headline: str
    titles: dict[str, tuple[str, str, int]]  # token -> (title, kind, tmdb_id), in screen order


def _fill(value, values: dict[str, str]):
    """`values` placeholders replaced in every string of a strings-file value (scalars, lists such as `hints`)."""
    if isinstance(value, str):
        for placeholder, text in values.items():
            value = value.replace(placeholder, text)
        return value
    if isinstance(value, list):
        return [_fill(v, values) for v in value]
    return value


def load_hello(cfg: Config, content_dir: Path = CONTENT_DIR) -> dict:
    """Payload of the `hello` message: assistant name, theme tokens, UI strings for the configured language (with
    {assistant} and {vpn} filled from [assistant]; {service}, {title} and {names} are the TV's to fill), the kids
    hours and the limits the TV mirrors (its ask deadlines, keyboard and recording caps)."""
    theme = json.loads((content_dir / "theme.json").read_text(encoding="utf-8"))
    strings_path = content_dir / f"strings.{cfg.language.code}.json"
    if not strings_path.exists():
        raise ValueError(f"invalid configuration: language.code: no {strings_path.name} in {content_dir}")
    names = {"{assistant}": cfg.assistant.name, "{vpn}": cfg.assistant.vpn_name}
    strings = {k: _fill(v, names) for k, v in json.loads(strings_path.read_text(encoding="utf-8")).items()}
    return {
        "assistant_name": cfg.assistant.name, "theme": theme, "strings": strings,
        # the TV defaults "who's watching" to everyone inside these hours, the grown-ups outside them
        "kids_hours": {"from": cfg.kids.default_from, "until": cfg.kids.default_until},
        "limits": {
            "ask_timeout_s": cfg.ask.timeout_s, "max_question_chars": cfg.ask.max_question_chars,
            "max_audio_s": cfg.speech.max_audio_s, "speech_timeout_s": cfg.speech.timeout_s,
        },
    }  # fmt: skip


def length_label(pick: Pick) -> str | None:
    if pick.kind == "tv":
        if pick.binge_seasons and pick.binge_minutes:  # the span that fit the hours the user asked for
            span = "S1" if pick.binge_seasons == 1 else f"S1–{pick.binge_seasons}"
            return f"{span} ≈ {max(1, round(pick.binge_minutes / 60))}h"
        return f"≈ {max(1, round(pick.total_minutes / 60))}h" if pick.total_minutes else None
    if not pick.minutes:
        return None
    h, m = divmod(pick.minutes, 60)
    if not h:
        return f"{m}m"
    return f"{h}h {m}m" if m else f"{h}h"


class TvHub:
    def __init__(
        self,
        server_token: str,
        ack_timeout_s: float,
        hello: dict,
        services: dict[str, Service],
        open_cfg: Open = DEFAULT_OPEN,
    ):
        self._token = server_token.encode()
        self._open_cfg = open_cfg
        self._ack_timeout_s = ack_timeout_s
        self._hello = hello
        # whether this server can transcribe spoken questions now (`hello` `speech`): the TV then sends the remote's
        # audio here (`remote_mic`) instead of using its own recognizer
        self.speech: Callable[[], bool] = lambda: False
        self._services = services
        self._ws: WebSocket | None = None
        self._state = TvState()
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._tasks: set[asyncio.Task] = set()
        self.on_opened: Callable[[str, str | None], Awaitable[None]] | None = None  # (token, service opened)
        # services the family opened or watched a pick on (store.services_used): those open first
        self.services_used: Callable[[Pick], set[str]] = lambda pick: set()
        # services that lately failed to open or play a pick (store.bad_services): those open last
        self.services_bad: Callable[[Pick], set[str]] = lambda pick: set()
        # (token, service, outcome): what happened when the TV opened a pick: failed, played or no_play
        self.on_outcome: Callable[[str, str, str], None] | None = None
        # who watches this TV (sent in `hello`), and saving a new list from the TV (returns what was saved)
        self.viewers: Callable[[], list[dict]] = lambda: []
        self._last_viewers: list[dict] = []
        self.on_viewers_set: Callable[[list], list[dict]] | None = None
        self.on_rendered: Callable[[str, int, dict[str, int]], Awaitable[None]] | None = None
        self.on_event: Callable[..., None] | None = None  # (event, **fields); TV status only, never tokens
        self._answer_turns: dict[str, str | None] = {}
        self._shown: OrderedDict[str, _Shown] = OrderedDict()  # answer_id -> what it drew, last ANSWERS_KEPT
        self.on_ask_timing: Callable[[str, dict[str, int]], None] | None = None  # (ask_id, {AskStage: ms})
        self.on_ask_cancel: Callable[[str], Awaitable[None]] | None = None  # (ask_id)
        self.on_ask_warm: Callable[[str], None] | None = None  # (ask_id); must not block: it starts a warm-up
        self.websocket_route = WebSocketRoute("/tv", self._endpoint)

    @property
    def state(self) -> TvState:
        return TvState(**vars(self._state))

    # ---- server -> TV ----

    def _hello_msg(self) -> dict:
        return {"type": "hello", **self._hello, "speech": self.speech(), "viewers": self._viewers_now()}

    def announce(self) -> None:
        """Say `hello` again to the connected TV: `speech` changed (the model finished loading). The TV applies a
        repeated hello like the first. Fire and forget."""
        self._fire(self._hello_msg())

    def _viewers_now(self) -> list[dict]:
        """The household for `hello` and `viewers`. When it can't be read, the list last read (an empty list would
        tell the TV nobody lives here)."""
        try:
            self._last_viewers = self.viewers()
        except Exception:
            log.exception("viewers unavailable; sending the last known list (%d)", len(self._last_viewers))
        return list(self._last_viewers)

    def _unreachable(self) -> Literal["tv_off", "offline"]:
        """No TV link: tv_off when the TV's last word was that its screen went off (standby cuts the network)."""
        return "offline" if self._state.screen_on else "tv_off"

    def _search_hint(self, pick: Pick, service: str) -> dict:
        """`search_hint` for services whose app opens on an empty search screen: the TV shows the title to type."""
        return {"search_hint": pick.title} if self._services[service].search_hint else {}

    def _launch_fields(self, pick: Pick, ids: dict[str, str] | None, service: str) -> dict:
        """`launches` (the ordered steps) plus the same steps as the legacy `launch` fallback chain."""
        steps = launch_steps(pick, self._services, ids, self._open_cfg, service=service)
        return {
            "launches": [s.model_dump(exclude={"fallback"}) for s in steps],
            "launch": launch_chain(steps).model_dump(),
        }

    def providers(self, pick: Pick, ids: dict[str, str] | None) -> list[str]:
        """The pick's services, best to open first (services.provider_order with the TV's current VPN state)."""
        try:
            used, bad = self.services_used(pick), self.services_bad(pick)
        except Exception:
            log.exception("services_used/services_bad failed")
            used, bad = set(), set()
        return provider_order(pick, self._services, used, ids, self._state.vpn, bad)

    def _provider(self, pick: Pick, ids: dict[str, str] | None, service: str) -> dict:
        """One way to open the pick: its service, label, launch steps and search hint."""
        return {
            "service": service, "service_label": self._services[service].label,
            **self._launch_fields(pick, ids, service), **self._search_hint(pick, service),
        }  # fmt: skip

    async def show(
        self,
        question: str,
        headline: str,
        subline: str,
        picks: list[Pick],
        ids: dict[str, dict[str, str]] | None = None,
        ask_id: str | None = None,
        reasons: dict[str, str] | None = None,
    ) -> ShowResult:
        """`ids`: token -> the pick's resolved title ids (title_ids.py) for its launch steps.
        `ask_id`: the TV ask this answers (sent only when given). `reasons`: token -> why it was picked."""
        ws = self._ws
        if ws is None:
            return self._unreachable()
        if not self._state.screen_on:
            return "tv_off"
        entries = []
        shown = _Shown(headline, {})
        for p in picks:
            if not p.services:
                log.warning("skipping pick %s: no service to launch", p.token)
                continue
            pick_ids = (ids or {}).get(p.token)
            providers = [self._provider(p, pick_ids, s) for s in self.providers(p, pick_ids)]
            # the best provider's fields at the top level too: OK opens it
            entry = {
                "token": p.token,
                "title": p.title,
                "poster_url": p.poster_url,
                **providers[0],
                "providers": providers,
            }
            label = length_label(p)
            if label:
                entry["length_label"] = label.replace(" ", NBSP)
            if (reasons or {}).get(p.token):
                entry["reason"] = reasons[p.token]
            entries.append(entry)
            shown.titles[p.token] = (p.title, p.kind, p.tmdb_id)
        answer_id = secrets.token_urlsafe(8)
        self._answer_turns[answer_id] = max(picks, key=lambda p: p.seq).turn_id if picks else None
        while len(self._answer_turns) > 50:
            del self._answer_turns[next(iter(self._answer_turns))]
        self._shown[answer_id] = shown
        while len(self._shown) > ANSWERS_KEPT:
            self._shown.popitem(last=False)
        msg = {"type": "show", "answer_id": answer_id, "question": question, "headline": headline, "subline": subline,
               "picks": entries}  # fmt: skip
        if ask_id is not None:
            msg["ask_id"] = ask_id
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending[answer_id] = fut
        try:
            async with asyncio.timeout(self._ack_timeout_s):
                await ws.send_json(msg)
                mode = await fut
        except TimeoutError:  # the link is up but the app sent no `shown`: say so, or "offline" points at the network
            log.warning("show %s: no shown ack from the TV within %.1f s", answer_id, self._ack_timeout_s)
            return "offline"
        except _SOCKET_GONE:
            return "offline"
        except Exception:  # a bug in the payload must not pass for a TV that is unreachable
            log.exception("show %s failed before the TV could answer", answer_id)
            return "offline"
        finally:
            self._pending.pop(answer_id, None)
        return mode if mode in SHOWN_MODES else "offline"  # type: ignore[return-value]

    def _emit(self, event: str, **fields) -> None:
        if self.on_event:
            try:
                self.on_event(event, **fields)
            except Exception:
                log.exception("event hook failed")

    def answer_context(self, answer_id: str | None, focused_token: str | None) -> ScreenContext | None:
        """What answer `answer_id` showed (one of the last ANSWERS_KEPT), with the title of the focused poster;
        None for an unknown answer. Not consumed on read."""
        shown = self._shown.get(answer_id) if answer_id else None
        if shown is None:
            return None
        focused = shown.titles.get(focused_token) if focused_token else None
        return ScreenContext(shown.headline, list(shown.titles.values()), focused[0] if focused else None)

    def turn_for(self, answer_id: str) -> str | None:
        """The turn an answer belonged to (for TV-reported timings); consumed on read."""
        return self._answer_turns.pop(answer_id, None)

    async def open(self, pick: Pick, ids: dict[str, str] | None = None, service: str | None = None) -> OpenResult:
        """Sends `open` for `service` (one of the pick's; default: the best, [providers]). The VPN state never
        blocks it; see [vpn_warning]."""
        ws = self._ws
        if ws is None:
            return self._unreachable()
        if not self._state.screen_on:
            return "tv_off"
        sid = service or self.providers(pick, ids)[0]
        msg = {"type": "open", "title": pick.title, **self._provider(pick, ids, sid)}
        del msg["service"]  # `open` names the app by its label only
        try:
            async with asyncio.timeout(self._ack_timeout_s):
                await ws.send_json(msg)
        except (TimeoutError, *_SOCKET_GONE):
            return "offline"
        except Exception:
            log.exception("open %s failed before it reached the TV", pick.token)
            return "offline"
        return "sent"

    def vpn_warning(self, pick: Pick, service: str | None = None) -> str | None:
        """A short note when the TV's last reported VPN state is wrong for `service` (default: the pick's primary),
        else None (also for a pick on no service)."""
        sid = service or pick.primary_service
        if sid is None:
            return None
        svc = self._services[sid]
        if svc.vpn == "on" and not self._state.vpn:
            return f"VPN is off; {svc.label} needs it on"
        if svc.vpn == "off" and self._state.vpn:
            return f"VPN is on; {svc.label} may not work"
        return None

    def looking(self, turn_id: str, question: str, ask_id: str | None = None) -> None:
        """Fire and forget; never blocks or raises. Without `ask_id` (Claude Desktop) it is not sent while something
        may be playing; with one the user just pressed the mic, so the TV is on Lumi's screen and it is sent."""
        if not self._state.screen_on or (ask_id is None and self._state.maybe_playing):
            return
        msg = {"type": "looking", "turn_id": turn_id, "question": question}
        if ask_id is not None:
            msg["ask_id"] = ask_id
        self._fire(msg)

    def preview(self, ask_id: str, cards: list) -> None:
        """The likely posters for ask `ask_id`, sent as soon as find_titles has them: the TV's Looking screen shows
        them while Claude chooses; the answer replaces them. Fire and forget."""
        if not self._state.screen_on:
            return
        self._fire({
            "type": "preview", "ask_id": ask_id,
            "posters": [{"title": c.title, "poster_url": c.poster_url} for c in cards],
        })  # fmt: skip

    def heard(self, ask_id: str, text: str) -> None:
        """What Lumi heard; the TV shows "Looking for “text”". Fire and forget."""
        self._fire({"type": "heard", "ask_id": ask_id, "text": text})

    def ask_error(self, ask_id: str, code: AskErrorCode) -> None:
        """The ask ended without an answer. Fire and forget."""
        self._fire({"type": "ask_error", "ask_id": ask_id, "code": code})

    def ask_reply(self, ask_id: str, text: str) -> None:
        """Claude answered in words, without picks. Fire and forget."""
        self._fire({"type": "ask_reply", "ask_id": ask_id, "text": text})

    def _fire(self, msg: dict) -> None:
        """Send without waiting; dropped when no TV is connected. Never blocks or raises."""
        try:
            ws = self._ws
            if ws is None:
                return
            task = asyncio.get_running_loop().create_task(self._send_quietly(ws, msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception:
            log.exception("%s failed", msg.get("type"))

    @staticmethod
    async def _send_quietly(ws: WebSocket, msg: dict) -> None:
        try:
            await ws.send_json(msg)
        except Exception:
            pass

    # ---- TV -> server ----

    async def _endpoint(self, ws: WebSocket) -> None:
        await ws.accept()
        # the token comes in the Authorization header only: URLs end up in logs, proxies and crash reports
        scheme, _, supplied = ws.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip().encode(), self._token):
            await ws.close(CLOSE_BAD_TOKEN)
            return
        try:
            # hello goes out before the socket is published, so show/open/looking can't overtake it
            await ws.send_json(self._hello_msg())
            old = self._ws
            self._ws = ws
            self._state = TvState(connected=True)
            self._emit("tv_connected")
            if old is not None:
                self._resolve_pending("offline")
                try:
                    await old.close(CLOSE_REPLACED)
                except Exception:
                    pass
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                text = message.get("text")
                if text is None:  # a binary frame: not this protocol (receive_text would raise KeyError here)
                    log.warning("the TV sent a binary frame; closing the link (%d)", CLOSE_UNSUPPORTED_DATA)
                    await ws.close(CLOSE_UNSUPPORTED_DATA)
                    break
                await self._handle(ws, text)
        except (WebSocketDisconnect, WebSocketDisconnected):
            pass
        finally:
            if self._ws is ws:
                self._ws = None
                self._state = TvState(screen_on=self._state.screen_on)  # remembered: a TV that slept says tv_off
                self._emit("tv_disconnected")
                self._resolve_pending("offline")

    def _resolve_pending(self, mode: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result(mode)

    async def _handle(self, ws: WebSocket, raw: str) -> None:
        if ws is not self._ws:  # replaced connection: ignore
            return
        try:
            msg = json.loads(raw)
            kind = msg["type"]
            if kind == "state":
                screen_on, playing, vpn = _bool(msg["screen_on"]), _bool(msg["playing"]), _bool(msg["vpn"])
                known = _bool(msg.get("playing_known", True))  # absent: an older app that always knows
                new = (screen_on, playing, vpn, known)
                s = self._state
                changed = (s.screen_on, s.playing, s.vpn, s.playing_known) != new
                s.screen_on, s.playing, s.vpn, s.playing_known = new
                if changed:
                    self._emit("tv_state", screen_on=screen_on, playing=playing, vpn=vpn, playing_known=known)
            elif kind == "shown":
                fut = self._pending.get(msg["answer_id"])
                if fut and not fut.done():
                    fut.set_result(msg["mode"])
            elif kind == "opened":
                if self.on_opened:
                    service = msg.get("service")
                    await self.on_opened(str(msg["token"]), str(service) if service else None)
            elif kind == "viewers_set":
                if self.on_viewers_set and isinstance(msg.get("viewers"), list):
                    try:
                        saved = self.on_viewers_set(msg["viewers"])
                    except Exception:
                        log.warning("viewers_set refused (bad entry)")
                        saved = self._viewers_now()
                    await ws.send_json({"type": "viewers", "viewers": saved})
            elif kind in ("open_failed", "play_check"):
                outcome = "failed" if kind == "open_failed" else ("played" if msg.get("played") else "no_play")
                if self.on_outcome and msg.get("token") and msg.get("service"):
                    self.on_outcome(str(msg["token"]), str(msg["service"]), outcome)
            elif kind == "rendered":
                if self.on_rendered:
                    await self.on_rendered(str(msg["answer_id"]), _ms(msg["ms"]), _phases(msg.get("phases")))
            elif kind == "ask_timing":
                ask_id = str(msg["ask_id"])
                timings = {stage: _ms(msg[field]) for field, stage in ASK_TIMING_FIELDS.items() if field in msg}
                if self.on_ask_timing and timings:
                    self.on_ask_timing(ask_id, timings)
            elif kind == "ask_warm":
                if self.on_ask_warm:
                    self.on_ask_warm(str(msg["ask_id"]))
            elif kind == "ask_cancel":
                if self.on_ask_cancel:
                    await self.on_ask_cancel(str(msg["ask_id"]))
            else:
                log.warning("unknown TV message type %r", kind)
        except Exception:
            log.exception("bad TV message or hook failure: %.200s", raw)


def _phases(raw) -> dict[str, int]:
    """A `rendered` message's per-step times; unknown names and values that aren't whole ms are dropped."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name in RENDER_PHASES:
        try:
            out[name] = _ms(raw[name])
        except (KeyError, ValueError):
            pass
    return out


def _ms(v) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ValueError(f"expected milliseconds, got {v!r}")
    return v


def _bool(v) -> bool:
    if not isinstance(v, bool):
        raise ValueError(f"expected bool, got {v!r}")
    return v
