import asyncio
import functools
import itertools
import json
import time
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lumitv.catalog import Pick
from lumitv.config import LaunchCfg
from lumitv.services import Service
from lumitv.tvlink import ScreenContext, TvHub

TOKEN = "s3cret"
HELLO = {"assistant_name": "Lumi", "theme": {"bg": "#08090a"}, "strings": {"looking": "Looking for…"}}
SERVICES = {
    "netflix": Service(
        "netflix",
        "Netflix",
        frozenset({8}),
        "AE",
        "com.netflix.ninja",
        "off",
        LaunchCfg(action="android.intent.action.VIEW", data="https://www.netflix.com/search?q={title_url}"),
    ),
    "videoland": Service("videoland", "Videoland", frozenset({72}), "NL", "nl.videoland", "on"),
    "prime": Service(
        "prime",
        "Prime Video",
        frozenset({119}),
        "AE",
        "com.prime",
        "any",
        LaunchCfg(action="android.intent.action.VIEW", data="https://p.test/search"),
        search_hint=True,
        google_tv=True,
    ),
}


_seq = itertools.count(1)


def _pick(token="tok1", kind="movie", services=("netflix",), minutes=112, total=None, **kw):
    return Pick(
        token=token,
        tmdb_id=1,
        kind=kind,
        title="Heat",
        year=1995,
        overview="",
        poster_url="http://p/1.jpg",
        services=list(services),
        certification="16",
        minutes=minutes,
        total_minutes=total,
        seasons=None,
        seen=False,
        kids_mode=False,
        kids_age=None,
        **{"seq": next(_seq), **kw},
    )


def _hub(ack=0.5):
    return TvHub(TOKEN, ack, HELLO, SERVICES)


HANG_S = 30.0  # tests wait on outcomes with this guard; nothing here should take seconds


class Rig:
    """Runs the hub's app in TestClient's loop; hub coroutines are driven through the same portal."""

    def __init__(self, hub):
        self.hub = hub
        self.client = TestClient(Starlette(routes=[hub.websocket_route]))

    def __enter__(self):
        self.client.__enter__()
        return self

    def __exit__(self, *a):
        self.client.__exit__(*a)

    def connect(self, token=TOKEN):
        ws = self.client.websocket_connect("/tv", headers={"Authorization": f"Bearer {token}"})
        return ws

    def start(self, fn, *args):
        return self.client.portal.start_task_soon(fn, *args)

    def call(self, fn, *args):
        return self.client.portal.call(fn, *args)

    def wait_for(self, cond, timeout=HANG_S):
        """Block until `cond()` holds (polled on the hub's loop): the message was handled, the hook ran. A hang guard,
        not a wall-clock budget."""

        async def poll():
            async with asyncio.timeout(timeout):
                while not cond():
                    await asyncio.sleep(0.005)

        self.call(poll)

    def settle(self):
        """Only for a check that nothing happened on another socket, where there is nothing to wait for."""
        self.call(asyncio.sleep, 0.05)


async def _sent(hub):
    """Wait for the hub's fire-and-forget sends (looking, preview, heard, ...) to finish."""
    await asyncio.gather(*list(hub._tasks), return_exceptions=True)


async def test_no_tv_returns_offline_without_waiting_for_an_ack():
    hub = _hub(ack=HANG_S)
    t = time.monotonic()
    assert await hub.show("q", "h", "s", [_pick()]) == "offline"
    assert time.monotonic() - t < HANG_S / 4  # at once: nothing to wait for, no ack timeout started


def test_looking_without_tv_is_a_no_op():
    assert _hub().looking("t1", "q") is None  # synchronous: it never waits


async def test_open_without_tv_is_offline():
    assert await _hub().open(_pick()) == "offline"


def test_bad_token_rejected():
    with Rig(_hub()) as rig:
        with pytest.raises(WebSocketDisconnect) as e:
            with rig.connect("wrong") as ws:
                ws.receive_json()
        assert e.value.code == 4401
        with pytest.raises(WebSocketDisconnect) as e:
            with rig.client.websocket_connect("/tv") as ws:
                ws.receive_json()
        assert e.value.code == 4401
        assert not rig.hub.state.connected


def test_hello_is_first_message():
    with Rig(_hub()) as rig, rig.connect() as ws:
        assert ws.receive_json() == {"type": "hello", **HELLO, "speech": False, "viewers": []}
        assert rig.hub.state.connected


def test_hello_speech_is_asked_at_each_connect():
    hub = _hub()
    with Rig(hub) as rig:
        with rig.connect() as ws:
            assert ws.receive_json()["speech"] is False
        hub.speech = lambda: True
        with rig.connect() as ws:
            assert ws.receive_json()["speech"] is True


def test_tv_off_not_sent():
    with Rig(_hub()) as rig, rig.connect() as ws:
        ws.receive_json()
        ws.send_json({"type": "state", "screen_on": False, "playing": False, "vpn": False})
        rig.wait_for(lambda: not rig.hub.state.screen_on)
        assert rig.call(rig.hub.show, "q", "h", "s", [_pick()]) == "tv_off"
        # nothing was sent: the next message the TV sees is the open we send afterwards
        assert rig.call(rig.hub.open, _pick()) == "tv_off"
        ws.send_json({"type": "state", "screen_on": True, "playing": False, "vpn": False})
        rig.wait_for(lambda: rig.hub.state.screen_on)
        assert rig.call(rig.hub.open, _pick()) == "sent"
        msg = ws.receive_json()
        assert msg["type"] == "open" and msg["service_label"] == "Netflix"
        assert msg["launch"]["data"] == "https://www.netflix.com/search?q=Heat"


def test_show_waits_for_ack():
    with Rig(_hub()) as rig, rig.connect() as ws:
        ws.receive_json()
        fut = rig.start(
            rig.hub.show,
            "what?",
            "Tonight",
            "three picks",
            [_pick("a"), _pick("b", kind="tv", services=("videoland", "netflix"), minutes=50, total=540)],
        )
        msg = ws.receive_json()
        assert msg["type"] == "show"
        assert msg["question"] == "what?" and msg["headline"] == "Tonight" and msg["subline"] == "three picks"
        a, b = msg["picks"]
        providers = a.pop("providers")
        assert providers == [{k: a[k] for k in ("service", "service_label", "launches", "launch")}]
        assert a == {
            "token": "a",
            "title": "Heat",
            "poster_url": "http://p/1.jpg",
            "service": "netflix",
            "service_label": "Netflix",
            "length_label": "1h 52m",  # non-breaking: one token on the card
            "launches": [
                {
                    "package": "com.netflix.ninja",
                    "action": "android.intent.action.VIEW",
                    "data": "https://www.netflix.com/search?q=Heat",
                    "extras": {},
                    "component": None,
                    "vpn": "off",
                    "hint": False,
                    "relaunch_after_s": [],
                },
                {
                    "package": "com.netflix.ninja",
                    "action": "android.intent.action.MAIN",
                    "data": None,
                    "extras": {},
                    "component": None,
                    "vpn": "off",
                    "hint": False,
                    "relaunch_after_s": [],
                },
            ],
            "launch": {
                "package": "com.netflix.ninja",
                "action": "android.intent.action.VIEW",
                "data": "https://www.netflix.com/search?q=Heat",
                "extras": {},
                "component": None,
                "vpn": "off",
                "hint": False,
                "relaunch_after_s": [],
                "fallback": {
                    "package": "com.netflix.ninja",
                    "action": "android.intent.action.MAIN",
                    "data": None,
                    "extras": {},
                    "component": None,
                    "vpn": "off",
                    "hint": False,
                    "relaunch_after_s": [],
                    "fallback": None,
                },
            },
        }
        # the TV's VPN is off: Netflix (wants it off) opens first, Videoland (needs it on) is the second choice
        assert b["service"] == "netflix" and b["length_label"] == "≈ 9h" and b["launch"]["vpn"] == "off"
        assert [x["service"] for x in b["providers"]] == ["netflix", "videoland"]
        assert b["providers"][1]["launch"]["vpn"] == "on"
        ws.send_json({"type": "shown", "answer_id": msg["answer_id"], "mode": "notice"})
        assert fut.result(timeout=2) == "notice"


def test_answer_turn_is_the_newest_picks_turn_when_clock_ties():
    with Rig(_hub()) as rig, rig.connect() as ws:
        ws.receive_json()
        old, new = _pick("old", turn_id="t1"), _pick("new", turn_id="t2")
        assert old.issued_at == new.issued_at  # tied clock; only seq tells them apart
        rig.start(rig.hub.show, "q", "h", "s", [old, new])
        msg = ws.receive_json()
        assert rig.hub.turn_for(msg["answer_id"]) == "t2"


def test_length_label_omitted_when_unknown():
    with Rig(_hub()) as rig, rig.connect() as ws:
        ws.receive_json()
        rig.start(rig.hub.show, "q", "h", "s", [_pick(minutes=None), _pick("t", kind="tv", minutes=40, total=None)])
        msg = ws.receive_json()
        assert all("length_label" not in p for p in msg["picks"])


def test_show_sends_each_picks_reason_only_when_given():
    with Rig(_hub()) as rig, rig.connect() as ws:
        ws.receive_json()
        rig.start(
            functools.partial(rig.hub.show, reasons={"a": "Trek itself"}), "q", "h", "s", [_pick("a"), _pick("b")]
        )
        a, b = ws.receive_json()["picks"]
        assert a["reason"] == "Trek itself" and "reason" not in b


def test_show_ack_for_other_answer_ignored_then_timeout(caplog):
    with Rig(_hub(ack=0.3)) as rig, rig.connect() as ws:
        ws.receive_json()
        fut = rig.start(rig.hub.show, "q", "h", "s", [_pick()])
        answer_id = ws.receive_json()["answer_id"]
        ws.send_json({"type": "shown", "answer_id": "bogus", "mode": "screen"})
        assert fut.result(timeout=2) == "offline"
    # the TV was connected: the log says it did not ack, so nobody goes looking for a network problem
    assert f"show {answer_id}: no shown ack from the TV within 0.3 s" in caplog.text


def test_ack_timeout_returns_offline():
    """A TV that never acks: offline once the ack timeout passes (the configured [timeouts] tv_ack_s; short here)."""
    with Rig(_hub(ack=0.3)) as rig, rig.connect() as ws:
        ws.receive_json()
        fut = rig.start(rig.hub.show, "q", "h", "s", [_pick()])
        ws.receive_json()  # the TV never answers
        assert fut.result(timeout=HANG_S) == "offline"


def test_disconnect_during_show_returns_offline():
    with Rig(_hub(ack=HANG_S)) as rig:
        with rig.connect() as ws:
            ws.receive_json()
            fut = rig.start(rig.hub.show, "q", "h", "s", [_pick()])
            ws.receive_json()
        assert fut.result(timeout=HANG_S) == "offline"  # the disconnect resolves it, not the ack timeout
        assert not rig.hub.state.connected


def test_opened_calls_hook():
    got = []

    async def on_opened(token, service):
        got.append((token, service))

    hub = _hub()
    hub.on_opened = on_opened
    with Rig(hub) as rig, rig.connect() as ws:
        ws.receive_json()
        ws.send_json({"type": "opened", "token": "tok9"})
        ws.send_json({"type": "opened", "token": "tok8", "service": "prime"})
        rig.wait_for(lambda: len(got) == 2)
    assert got == [("tok9", None), ("tok8", "prime")]


def test_rendered_calls_hook():
    got = []

    async def on_rendered(answer_id, ms, phases):
        got.append((answer_id, ms, phases))

    hub = _hub()
    hub.on_rendered = on_rendered
    with Rig(hub) as rig, rig.connect() as ws:
        ws.receive_json()
        ws.send_json({"type": "rendered", "answer_id": "x", "ms": 420})
        # the TV's per-step times: known names with whole non-negative ms; anything else is dropped
        ws.send_json(
            {
                "type": "rendered",
                "answer_id": "y",
                "ms": 900,
                "phases": {"start": 5, "created": 400.6, "frame": 700, "bogus": 1, "first": -3, "posted": True},
            }
        )
        ws.send_json({"type": "rendered", "answer_id": "z", "ms": 50, "phases": "nonsense"})
        for bad in (True, -5, "12", 1e9):  # `ms` follows the same rule as the phases: whole non-negative ms, or dropped
            ws.send_json({"type": "rendered", "answer_id": "bad", "ms": bad})
        ws.send_json({"type": "rendered", "answer_id": "w", "ms": 1})  # handled after the bad ones: they are done too
        rig.wait_for(lambda: len(got) == 4)
        assert rig.hub.state.connected
    assert got == [("x", 420, {}), ("y", 900, {"start": 5, "frame": 700}), ("z", 50, {}), ("w", 1, {})]


def test_failing_hook_and_garbage_do_not_drop_socket():
    async def boom(token, service):
        raise RuntimeError("x")

    hub = _hub()
    hub.on_opened = boom
    with Rig(hub) as rig, rig.connect() as ws:
        ws.receive_json()
        ws.send_text("not json")
        ws.send_json([1, 2])
        ws.send_json({"type": "nope"})
        ws.send_json({"type": "opened", "token": "t"})
        ws.send_json({"type": "state", "screen_on": True, "playing": True, "vpn": False})  # handled after all of those
        rig.wait_for(lambda: rig.hub.state.playing)
        assert rig.hub.state.connected
        assert rig.call(rig.hub.open, _pick()) == "sent"
        assert ws.receive_json()["type"] == "open"


def test_state_tracked():
    with Rig(_hub()) as rig, rig.connect() as ws:
        ws.receive_json()
        ws.send_json({"type": "state", "screen_on": True, "playing": True, "vpn": True})
        rig.wait_for(lambda: rig.hub.state.vpn)
        s = rig.hub.state
        assert (s.connected, s.screen_on, s.playing, s.vpn) == (True, True, True, True)
    assert not rig.hub.state.connected  # the client's exit waits for the app to see the disconnect


def test_looking_sent_when_idle():
    with Rig(_hub()) as rig, rig.connect() as ws:
        _idle(rig, ws)
        rig.call(_looking, rig.hub)
        assert ws.receive_json() == {"type": "looking", "turn_id": "t1", "question": "q"}


def test_looking_not_sent_while_playing():
    with Rig(_hub()) as rig, rig.connect() as ws:
        _idle(rig, ws, playing=True)
        rig.call(_looking, rig.hub)
        assert rig.call(rig.hub.open, _pick()) == "sent"
        assert ws.receive_json()["type"] == "open"  # looking never arrived


def test_looking_not_sent_when_screen_off():
    with Rig(_hub()) as rig, rig.connect() as ws:
        ws.receive_json()
        _state(ws, rig, screen_on=False)
        rig.call(_looking, rig.hub)
        _state(ws, rig, screen_on=True)
        assert rig.call(rig.hub.open, _pick()) == "sent"
        assert ws.receive_json()["type"] == "open"


def test_looking_swallows_closed_socket():
    with Rig(_hub()) as rig:
        with rig.connect() as ws:
            ws.receive_json()
        rig.call(_looking, rig.hub)  # must not raise


def test_new_connection_replaces_old():
    from lumitv.tvlink import CLOSE_REPLACED

    with Rig(_hub()) as rig:
        with rig.connect() as old:
            old.receive_json()
            with rig.connect() as new:
                new.receive_json()
                assert rig.call(rig.hub.open, _pick()) == "sent"
                assert new.receive_json()["type"] == "open"
                with pytest.raises(WebSocketDisconnect) as e:
                    old.receive_json()
                assert e.value.code == CLOSE_REPLACED == 4000  # protocol.md: the TV keeps its backoff on this code


def test_binary_frame_is_a_protocol_error_not_a_traceback():
    """A binary frame closes the link with 1003 (unsupported data) and leaves the hub ready for the next connect."""
    from lumitv.tvlink import CLOSE_UNSUPPORTED_DATA

    with Rig(_hub()) as rig:
        with pytest.raises(WebSocketDisconnect) as e:
            with rig.connect() as ws:
                ws.receive_json()
                ws.send_bytes(b"\x00\x01")
                ws.receive_json()
        assert e.value.code == CLOSE_UNSUPPORTED_DATA == 1003
        assert not rig.hub.state.connected
        with rig.connect() as ws:
            assert ws.receive_json()["type"] == "hello" and rig.hub.state.connected


class _BrokenWs:
    """A socket whose send fails for a reason that is not the network."""

    async def send_json(self, msg):
        raise TypeError("Object of type Pick is not JSON serializable")


async def test_show_and_open_log_an_unexpected_error_instead_of_blaming_the_network(caplog):
    hub = _hub(ack=0.3)
    hub._ws = _BrokenWs()
    assert await hub.show("q", "h", "s", [_pick()]) == "offline"
    assert await hub.open(_pick()) == "offline"
    assert "failed before the TV could answer" in caplog.text and "not JSON serializable" in caplog.text
    assert "open tok1 failed before it reached the TV" in caplog.text


def test_vpn_warning_is_none_for_a_pick_on_no_service():
    assert _hub().vpn_warning(_pick(services=())) is None


async def _looking(hub):
    hub.looking("t1", "q")
    await _sent(hub)


def test_load_hello_from_content(tmp_path):
    from lumitv import config
    from lumitv.tvlink import load_hello

    env = tmp_path / ".env"
    env.write_text("TMDB_TOKEN=a\nSERVER_TOKEN=b\n")
    cfg = config.load(Path(__file__).parent.parent / "lumitv.example.toml", env_file=env)
    h = load_hello(cfg)
    assert h["assistant_name"] == "Lumi"
    assert h["theme"]["bg"] == "#08090a" and h["theme"]["indigo"] == "#7170ff"
    assert h["strings"]["open"] == "Open in {service}"
    assert "{service}" in h["strings"]["needs_vpn_on"] and isinstance(h["strings"]["hints"], list)
    # the server's placeholders are filled before the strings leave; the TV's ({service}, {title}, {names}) stay
    flat = [s for v in h["strings"].values() for s in (v if isinstance(v, list) else [v])]
    assert not any("{assistant}" in s or "{vpn}" in s for s in flat)
    raw = json.loads((Path(__file__).parent.parent / "content" / "strings.en.json").read_text(encoding="utf-8"))
    if "{vpn}" in raw["needs_vpn_on"]:  # the shipped file names no VPN brand: [assistant] vpn_name fills it
        assert cfg.assistant.vpn_name in h["strings"]["needs_vpn_on"]
    if any("{assistant}" in s for s in raw.values() if isinstance(s, str)):
        assert any(cfg.assistant.name in s for s in flat)
    assert h["kids_hours"] == {"from": "06:00", "until": "19:00"}
    # the TV derives its deadlines and input caps from these ([ask] and [speech] in the example config)
    assert h["limits"] == {
        "ask_timeout_s": 45.0,
        "max_question_chars": 200,
        "max_audio_s": 15.0,
        "speech_timeout_s": 10.0,
    }
    cfg.ask.timeout_s, cfg.speech.max_audio_s = 90.0, 20.0
    assert load_hello(cfg)["limits"]["ask_timeout_s"] == 90.0 and load_hello(cfg)["limits"]["max_audio_s"] == 20.0
    cfg.language.code = "xx"
    with pytest.raises(ValueError, match=r"strings\.xx\.json"):
        load_hello(cfg)


def test_hello_strings_fill_the_assistant_and_vpn_names(tmp_path):
    """{assistant} and {vpn} come from [assistant] in every string value, list items included; {service} is the TV's."""
    from lumitv import config
    from lumitv.tvlink import load_hello

    env = tmp_path / ".env"
    env.write_text("TMDB_TOKEN=a\nSERVER_TOKEN=b\n")
    cfg = config.load(Path(__file__).parent.parent / "lumitv.example.toml", env_file=env)
    cfg.assistant.name, cfg.assistant.vpn_name = "Nova", "NordVPN"
    content = tmp_path / "content"
    content.mkdir()
    (content / "theme.json").write_text('{"bg": "#000"}', encoding="utf-8")
    (content / "strings.en.json").write_text(json.dumps({
        "idle": "Press the mic button to ask {assistant}.",
        "needs_vpn_on": "{service} needs {vpn} on",
        "hints": ["◀ ▶ choose", "OK asks {assistant}", "{vpn} off: {service}"],
        "offline": "{assistant}'s server isn't answering.",
    }), encoding="utf-8")  # fmt: skip
    strings = load_hello(cfg, content_dir=content)["strings"]
    assert strings["idle"] == "Press the mic button to ask Nova."
    assert strings["needs_vpn_on"] == "{service} needs NordVPN on"
    assert strings["hints"] == ["◀ ▶ choose", "OK asks Nova", "NordVPN off: {service}"]
    assert strings["offline"] == "Nova's server isn't answering."


class _HangingWs:
    async def send_json(self, msg):
        await asyncio.Event().wait()


async def test_show_with_hanging_send_returns_offline_at_the_ack_timeout():
    hub = _hub(ack=0.3)
    hub._ws = _HangingWs()
    assert await asyncio.wait_for(hub.show("q", "h", "s", [_pick()]), HANG_S) == "offline"  # the ack timeout cut it


async def test_open_with_hanging_send_returns_offline():
    hub = _hub(ack=0.3)
    hub._ws = _HangingWs()
    assert await asyncio.wait_for(hub.open(_pick()), HANG_S) == "offline"


def test_hello_first_even_when_looking_races_connect():
    async def spam(hub):
        for _ in range(200):
            hub.looking("t", "q")
            await asyncio.sleep(0)

    with Rig(_hub()) as rig:
        fut = rig.start(spam, rig.hub)
        with rig.connect() as ws:
            assert ws.receive_json()["type"] == "hello"
        fut.result(timeout=5)


def test_replaced_socket_messages_ignored():
    got = []

    async def on_opened(token):
        got.append(token)

    hub = _hub()
    hub.on_opened = on_opened
    with Rig(hub) as rig:
        with rig.connect() as old:
            old.receive_json()
            with rig.connect() as new:
                new.receive_json()
                try:
                    old.send_json({"type": "opened", "token": "stale"})
                    old.send_json({"type": "state", "screen_on": False, "playing": True, "vpn": True})
                except Exception:
                    pass
                rig.settle()  # the stale socket's messages leave no trace to wait for
                assert got == [] and rig.hub.state.screen_on and not rig.hub.state.playing


def test_length_label_names_the_matched_span():
    from lumitv.tvlink import length_label

    whole = _pick(kind="tv", minutes=50, total=720, binge_seasons=None, binge_minutes=720)
    s1 = _pick(kind="tv", minutes=660, total=3000, binge_seasons=1, binge_minutes=660)
    s12 = _pick(kind="tv", minutes=300, total=3000, binge_seasons=2, binge_minutes=780)
    assert [length_label(p) for p in (whole, s1, s12)] == ["≈ 12h", "S1 ≈ 11h", "S1–2 ≈ 13h"]
    assert length_label(_pick(kind="tv", minutes=50, total=540)) == "≈ 9h"  # no hours filter: whole run


def test_show_sends_length_labels_that_never_wrap_mid_value():
    """On the card the label is one unbreakable token (non-breaking spaces), so a long service line can't leave
    "≈" on one line and "13h" on the next; Claude's `length` (tools.card_json) keeps plain spaces."""
    binge = _pick("b", kind="tv", minutes=300, total=3000, binge_seasons=2, binge_minutes=780)
    with Rig(_hub()) as rig, rig.connect() as ws:
        ws.receive_json()
        rig.start(rig.hub.show, "q", "h", "s", [_pick("a"), binge])
        a, b = ws.receive_json()["picks"]
        assert (a["length_label"], b["length_label"]) == ("1h 52m", "S1–2 ≈ 13h")
        assert " " not in a["length_label"] + b["length_label"]


def _state(ws, rig, screen_on=True, playing=False, vpn=False, **extra):
    """Send a `state` and wait until the hub shows it."""
    ws.send_json({"type": "state", "screen_on": screen_on, "playing": playing, "vpn": vpn, **extra})
    known = extra.get("playing_known", True)
    rig.wait_for(lambda: (rig.hub.state.screen_on, rig.hub.state.playing, rig.hub.state.vpn, rig.hub.state.playing_known)
                 == (screen_on, playing, vpn, known))  # fmt: skip


def test_open_always_sends_and_vpn_warning_reports_a_mismatch():
    with Rig(_hub()) as rig, rig.connect() as ws:
        ws.receive_json()
        _state(ws, rig, vpn=False)
        vl, nf, pr = _pick(services=("videoland",)), _pick(), _pick(services=("prime",))
        assert rig.call(rig.hub.open, vl) == "sent"
        assert ws.receive_json()["launch"]["vpn"] == "on"  # the TV shows its own warning
        assert rig.hub.vpn_warning(vl) == "VPN is off; Videoland needs it on"
        assert rig.call(rig.hub.open, nf) == "sent" and rig.hub.vpn_warning(nf) is None
        ws.receive_json()
        _state(ws, rig, vpn=True)
        assert rig.call(rig.hub.open, nf) == "sent"
        assert ws.receive_json()["type"] == "open"
        assert rig.hub.vpn_warning(nf) == "VPN is on; Netflix may not work"
        assert rig.hub.vpn_warning(vl) is None and rig.hub.vpn_warning(pr) is None  # videoland ok, prime any


def test_tv_that_left_with_screen_off_is_tv_off_not_offline():
    with Rig(_hub()) as rig:
        with rig.connect() as ws:
            ws.receive_json()
            _state(ws, rig, screen_on=False)
        rig.wait_for(lambda: not rig.hub.state.connected)
        assert rig.call(rig.hub.show, "q", "h", "s", [_pick()]) == "tv_off"
        assert rig.call(rig.hub.open, _pick()) == "tv_off"
        with rig.connect() as ws:  # back, screen on, then gone again
            ws.receive_json()
            _state(ws, rig, screen_on=True)
        rig.wait_for(lambda: not rig.hub.state.connected)
        assert rig.call(rig.hub.show, "q", "h", "s", [_pick()]) == "offline"


def test_unknown_playback_state_counts_as_playing():
    with Rig(_hub()) as rig, rig.connect() as ws:
        ws.receive_json()
        _state(ws, rig, playing=True, playing_known=False)
        assert rig.hub.state.playing_known is False and rig.hub.state.maybe_playing
        rig.call(_looking, rig.hub)
        assert rig.call(rig.hub.open, _pick()) == "sent"
        assert ws.receive_json()["type"] == "open"  # looking never arrived
        _state(ws, rig, playing=False)  # an app that doesn't send the field: known
        assert rig.hub.state.playing_known is True
        rig.call(_looking, rig.hub)
        assert ws.receive_json()["type"] == "looking"


def test_shown_tv_off_ack_is_tv_off():
    with Rig(_hub()) as rig, rig.connect() as ws:
        ws.receive_json()
        fut = rig.start(rig.hub.show, "q", "h", "s", [_pick()])
        msg = ws.receive_json()
        ws.send_json({"type": "shown", "answer_id": msg["answer_id"], "mode": "tv_off"})
        assert fut.result(timeout=2) == "tv_off"


def test_search_hint_only_for_configured_services():
    with Rig(_hub()) as rig, rig.connect() as ws:
        ws.receive_json()
        rig.call(rig.hub.open, _pick(services=("prime",)))
        assert ws.receive_json()["search_hint"] == "Heat"
        rig.call(rig.hub.open, _pick())
        assert "search_hint" not in ws.receive_json()
        fut = rig.start(rig.hub.show, "q", "h", "s", [_pick("a", services=("prime",)), _pick("b")])
        msg = ws.receive_json()
        a, b = msg["picks"]
        assert a["search_hint"] == "Heat" and "search_hint" not in b
        ws.send_json({"type": "shown", "answer_id": msg["answer_id"], "mode": "screen"})
        assert fut.result(timeout=2) == "screen"


def test_show_and_open_put_the_google_tv_page_first_when_resolved():
    with Rig(_hub()) as rig, rig.connect() as ws:
        ws.receive_json()
        rig.start(
            rig.hub.show,
            "q",
            "h",
            "s",
            [_pick("a", services=("prime",)), _pick("b", services=("prime",))],
            {"a": {"google_id": "/g/11f7dr1t_l"}},
        )
        a, b = ws.receive_json()["picks"]
        assert [s["package"] for s in a["launches"]] == [
            "com.google.android.apps.tv.launcherx",
            "com.prime",
            "com.prime",
        ]
        assert a["launches"][0]["data"] == "https://tv.google.com/asset/%2Fg%2F11f7dr1t_l"
        assert [s["hint"] for s in a["launches"]] == [False, True, False] and a["search_hint"] == "Heat"
        assert a["launch"]["package"] == "com.google.android.apps.tv.launcherx"  # legacy chain, same order
        assert a["launch"]["fallback"]["package"] == "com.prime"
        assert [s["package"] for s in b["launches"]] == ["com.prime", "com.prime"]  # unresolved: no Google step
        assert rig.call(rig.hub.open, _pick(services=("prime",)), {"google_id": "/m/0abc"}) == "sent"
        msg = ws.receive_json()
        assert msg["title"] == "Heat"
        assert msg["launches"][0]["data"] == "https://tv.google.com/asset/%2Fm%2F0abc"
        assert msg["service_label"] == "Prime Video"


# ---- asking from the TV ----


def _idle(rig, ws, playing=False):
    ws.receive_json()  # hello
    _state(ws, rig, playing=playing)


def test_show_carries_ask_id_only_when_given():
    with Rig(_hub()) as rig, rig.connect() as ws:
        _idle(rig, ws)
        rig.start(rig.hub.show, "q", "h", "s", [_pick()])
        assert "ask_id" not in ws.receive_json()
        rig.start(rig.hub.show, "q", "h", "s", [_pick()], None, "abcd1234")
        assert ws.receive_json()["ask_id"] == "abcd1234"


async def _looking_for_ask(hub):
    hub.looking("t1", "q", ask_id="abcd1234")


def test_looking_with_ask_id_sent_while_playing():
    with Rig(_hub()) as rig, rig.connect() as ws:
        _idle(rig, ws, playing=True)
        rig.call(_looking_for_ask, rig.hub)
        assert ws.receive_json() == {"type": "looking", "turn_id": "t1", "question": "q", "ask_id": "abcd1234"}


def test_looking_without_ask_id_still_suppressed_while_playing():
    with Rig(_hub()) as rig, rig.connect() as ws:
        _idle(rig, ws, playing=True)
        rig.call(_looking, rig.hub)
        assert rig.call(rig.hub.open, _pick()) == "sent"
        assert ws.receive_json()["type"] == "open"  # looking never arrived


async def _ask_messages(hub):
    hub.heard("abcd1234", "something fun")
    hub.ask_error("abcd1234", "timeout")
    hub.ask_reply("abcd1234", "Which of you is watching?")
    await _sent(hub)


def test_heard_error_reply_reach_the_tv():
    with Rig(_hub()) as rig, rig.connect() as ws:
        _idle(rig, ws)
        rig.call(_ask_messages, rig.hub)
        assert ws.receive_json() == {"type": "heard", "ask_id": "abcd1234", "text": "something fun"}
        assert ws.receive_json() == {"type": "ask_error", "ask_id": "abcd1234", "code": "timeout"}
        assert ws.receive_json() == {"type": "ask_reply", "ask_id": "abcd1234", "text": "Which of you is watching?"}


async def test_ask_messages_without_tv_do_not_raise():
    hub = _hub()
    await _ask_messages(hub)
    hub.looking("t1", "q", ask_id="abcd1234")
    assert hub._tasks == set()  # nothing was queued: there is no socket to send to


def test_ask_messages_swallow_closed_socket():
    with Rig(_hub()) as rig:
        with rig.connect() as ws:
            _idle(rig, ws)
            hub_ws = rig.hub._ws
        rig.wait_for(lambda: not rig.hub.state.connected)
        rig.hub._ws = hub_ws  # a socket the TV already closed
        rig.call(_ask_messages, rig.hub)  # the sends fail quietly: nothing raises, nothing is left running
        assert rig.hub._tasks == set()


def test_answer_context_lists_titles_and_focus():
    picks = [
        _pick("a").model_copy(update={"title": "Heat", "tmdb_id": 949}),
        _pick("b", kind="tv", minutes=50, total=540).model_copy(update={"title": "Dark", "tmdb_id": 70523}),
        _pick("c").model_copy(update={"title": "Up", "tmdb_id": 14160}),
    ]
    with Rig(_hub()) as rig, rig.connect() as ws:
        _idle(rig, ws)
        rig.start(rig.hub.show, "q", "Tonight", "s", picks)
        answer_id = ws.receive_json()["answer_id"]
        ctx = rig.hub.answer_context(answer_id, picks[1].token)
        assert ctx == ScreenContext(
            "Tonight", [("Heat", "movie", 949), ("Dark", "tv", 70523), ("Up", "movie", 14160)], focused="Dark"
        )
        assert rig.hub.answer_context(answer_id, "nope") == ScreenContext("Tonight", ctx.titles, focused=None)
        assert rig.hub.answer_context(answer_id, None).focused is None
        assert rig.hub.answer_context(answer_id, picks[0].token).focused == "Heat"  # not consumed on read
        assert rig.hub.answer_context("unknown", picks[1].token) is None
        assert rig.hub.answer_context(None, None) is None


def test_answer_context_keeps_last_20_answers():
    with Rig(_hub()) as rig, rig.connect() as ws:
        _idle(rig, ws)
        ids = []
        for i in range(21):
            rig.start(rig.hub.show, "q", f"h{i}", "s", [_pick()])
            ids.append(ws.receive_json()["answer_id"])
        assert rig.hub.answer_context(ids[0], None) is None
        assert rig.hub.answer_context(ids[1], None).headline == "h1"
        assert rig.hub.answer_context(ids[20], None).headline == "h20"


def test_ask_timing_and_cancel_reach_hooks():
    timings, cancels = [], []

    async def on_cancel(ask_id):
        cancels.append(ask_id)

    hub = _hub()
    hub.on_ask_timing = lambda ask_id, ms: timings.append((ask_id, ms))
    hub.on_ask_cancel = on_cancel
    with Rig(hub) as rig, rig.connect() as ws:
        ws.receive_json()
        ws.send_json({"type": "ask_timing", "ask_id": "abcd1234", "listen_ms": 420, "heard_ms": 1800})
        ws.send_json({"type": "ask_timing", "ask_id": "efgh5678", "posters_ms": 9000, "typed_posters_ms": 7000})
        ws.send_json({"type": "ask_timing", "ask_id": "bad00000", "listen_ms": "fast"})  # ignored, socket stays
        ws.send_json({"type": "ask_cancel", "ask_id": "abcd1234"})
        rig.wait_for(lambda: cancels)  # the last message sent: the ones before it have been handled
        assert rig.hub.state.connected
    assert timings == [
        ("abcd1234", {"listen": 420, "heard": 1800}),
        ("efgh5678", {"posters": 9000, "typed_posters": 7000}),
    ]
    assert cancels == ["abcd1234"]


def test_open_on_a_named_service_and_history_puts_a_service_first():
    hub = _hub()
    hub.services_used = lambda pick: {"videoland"}
    with Rig(hub) as rig, rig.connect() as ws:
        ws.receive_json()
        _state(ws, rig, vpn=False)
        both = _pick(services=("netflix", "videoland"))
        assert hub.providers(both, None) == ["videoland", "netflix"]  # watched there before beats the VPN fit
        assert rig.call(functools.partial(hub.open, service="netflix"), both) == "sent"
        msg = ws.receive_json()
        assert msg["service_label"] == "Netflix" and "service" not in msg and msg["launch"]["vpn"] == "off"
        assert rig.call(hub.open, both) == "sent"
        assert ws.receive_json()["service_label"] == "Videoland"  # no service named: the best one


def test_token_in_the_url_is_refused():
    from lumitv.tvlink import CLOSE_BAD_TOKEN

    with Rig(_hub()) as rig:
        with pytest.raises(WebSocketDisconnect) as e:
            with rig.client.websocket_connect(f"/tv?token={TOKEN}") as ws:
                ws.receive_json()  # the token only counts in the Authorization header
        assert e.value.code == CLOSE_BAD_TOKEN == 4401
        assert not rig.hub.state.connected


def test_preview_sends_poster_titles_and_urls_only_while_the_screen_is_on():
    hub = _hub()
    with Rig(hub) as rig, rig.connect() as ws:
        ws.receive_json()
        _state(ws, rig, playing=False)
        rig.call(_preview, hub, [_pick("a"), _pick("b")])  # on the server's loop, as from find_titles
        msg = ws.receive_json()
        assert msg == {
            "type": "preview",
            "ask_id": "ask-1",
            "posters": [{"title": "Heat", "poster_url": "http://p/1.jpg"}] * 2,
        }


async def _preview(hub, cards):
    hub.preview("ask-1", cards)
    await _sent(hub)


def test_open_failures_and_play_checks_reach_the_outcome_hook():
    got = []
    hub = _hub()
    hub.on_outcome = lambda token, service, outcome: got.append((token, service, outcome))
    with Rig(hub) as rig, rig.connect() as ws:
        ws.receive_json()
        ws.send_json({"type": "open_failed", "token": "t1", "service": "netflix"})
        ws.send_json({"type": "play_check", "token": "t1", "service": "prime", "played": True})
        ws.send_json({"type": "play_check", "token": "t1", "service": "prime", "played": False})
        rig.wait_for(lambda: len(got) == 3)
    assert got == [("t1", "netflix", "failed"), ("t1", "prime", "played"), ("t1", "prime", "no_play")]


def test_viewers_come_in_hello_and_a_saved_list_is_echoed():
    hub = _hub()
    saved = []
    hub.viewers = lambda: [{"name": "Alex", "age": None}]
    hub.on_viewers_set = lambda raw: saved.extend(raw) or raw
    with Rig(hub) as rig, rig.connect() as ws:
        assert ws.receive_json()["viewers"] == [{"name": "Alex", "age": None}]
        ws.send_json({"type": "viewers_set", "viewers": [{"name": "Emma", "age": 6}]})
        assert ws.receive_json() == {"type": "viewers", "viewers": [{"name": "Emma", "age": 6}]}
    assert saved == [{"name": "Emma", "age": 6}]


def test_ask_warm_reaches_its_hook_without_waiting():
    warms = []
    hub = _hub()
    hub.on_ask_warm = warms.append  # sync: the socket never waits for the warm-up
    with Rig(hub) as rig, rig.connect() as ws:
        ws.receive_json()
        ws.send_json({"type": "ask_warm", "ask_id": "abcd1234"})
        ws.send_json({"type": "ask_warm"})  # malformed: ignored, socket stays
        ws.send_json({"type": "ask_warm", "ask_id": "efgh5678"})
        rig.wait_for(lambda: len(warms) == 2)
        assert rig.hub.state.connected
    assert warms == ["abcd1234", "efgh5678"]


def test_hello_keeps_the_last_known_household_when_the_store_fails(caplog):
    """A store that can't be read must not tell the TV that nobody lives here: the list last read is sent (an
    empty one only when there never was a successful read), and the failure is logged."""
    reads = [[{"name": "Alex", "age": None}], RuntimeError("database is locked"), [{"name": "Emma", "age": 6}]]

    def viewers():
        v = reads.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    hub = _hub()
    hub.viewers = viewers
    with Rig(hub) as rig:
        with rig.connect() as ws:
            assert ws.receive_json()["viewers"] == [{"name": "Alex", "age": None}]
        with rig.connect() as ws:
            assert ws.receive_json()["viewers"] == [{"name": "Alex", "age": None}]  # the store failed: last known
        assert "viewers unavailable; sending the last known list (1)" in caplog.text
        with rig.connect() as ws:
            assert ws.receive_json()["viewers"] == [{"name": "Emma", "age": 6}]
    fresh = _hub()
    fresh.viewers = _raise
    with Rig(fresh) as rig, rig.connect() as ws:
        assert ws.receive_json()["viewers"] == []


def _raise():
    raise RuntimeError("database is locked")
