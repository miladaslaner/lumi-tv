import asyncio
import io
import json
import logging
import os
import re
import shutil
import socket
import time
from pathlib import Path

import pytest
import uvicorn
import websockets
from starlette.testclient import TestClient

from lumitv import app as app_mod, config
from lumitv.store import Store
from test_tools import FakeSource, det

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "lumitv.example.toml"
INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}  # fmt: skip
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / ".env").write_text("TMDB_TOKEN=fake-tmdb\nSERVER_TOKEN=secret-server-token\n")
    c = config.load(EXAMPLE, env_file=tmp_path / ".env")
    c.server.db_path = tmp_path / "t.db"
    c.server.log_path = tmp_path / "logs" / "lumitv.jsonl"
    c.ask.workdir = tmp_path / "claude-home"
    c.ask.claude_path = str(tmp_path / "no-claude-here")  # tests never start the real Claude Code
    c.open.user_agent_contact = "lumi-tests@example.org"  # first-time setup done (doctor checks it)
    return c


def make_app(cfg):
    return app_mod.create_app(cfg, source=FakeSource({1: det("One")}))


def test_health_is_open(cfg):
    with TestClient(make_app(cfg)) as c:
        r = c.get("/health")
        assert r.status_code == 200 and r.text == "ok"


def test_mcp_requires_bearer(cfg):
    with TestClient(make_app(cfg)) as c:
        assert c.post("/mcp", json=INIT, headers=HEADERS).status_code == 401
        bad = {**HEADERS, "Authorization": "Bearer nope"}
        assert c.post("/mcp", json=INIT, headers=bad).status_code == 401
        ok = {**HEADERS, "Authorization": "Bearer secret-server-token"}
        r = c.post("/mcp", json=INIT, headers=ok)
        assert r.status_code == 200 and "Lumi" in r.text


def test_duplicate_service_ids_fail_startup(cfg):
    cfg.services.append(cfg.services[0])
    with pytest.raises(ValueError, match="duplicate"):
        make_app(cfg)


def _sse_json(r):
    lines = [ln[6:] for ln in r.text.splitlines() if ln.startswith("data: ")]
    return json.loads(lines[-1]) if lines else r.json()


def test_tv_opened_is_recorded(cfg):
    with TestClient(make_app(cfg)) as c:
        with c.websocket_connect("/tv", headers=tv_auth()) as ws:
            assert ws.receive_json()["type"] == "hello"
            h = {**HEADERS, "Authorization": "Bearer secret-server-token"}
            h["mcp-session-id"] = c.post("/mcp", json=INIT, headers=h).headers["mcp-session-id"]
            c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
            call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "find_titles", "arguments": {"question": "q"}}}  # fmt: skip
            payload = _sse_json(c.post("/mcp", json=call, headers=h))
            token = json.loads(payload["result"]["content"][0]["text"])["titles"][0]["token"]
            ws.send_json({"type": "opened", "token": token})
            for _ in range(50):
                if Store(cfg.server.db_path).taste_profile()["recently_opened"]:
                    break
                time.sleep(0.05)
    assert Store(cfg.server.db_path).taste_profile()["recently_opened"] == ["One"]


def test_log_masks_every_configured_secret_including_the_oauth_token(cfg):
    """A question that happens to contain a token (the TMDB token, the server token, the Claude OAuth token) lands in
    the JSONL log as ***."""
    from test_metrics import lines

    cfg.claude_oauth_token = "oat-secret-value"
    with TestClient(make_app(cfg)) as c:
        h = {**HEADERS, **AUTH}
        h["mcp-session-id"] = c.post("/mcp", json=INIT, headers=h).headers["mcp-session-id"]
        c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
        question = "fake-tmdb secret-server-token oat-secret-value"
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "find_titles", "arguments": {"question": question}}}  # fmt: skip
        assert c.post("/mcp", json=call, headers=h).status_code == 200
    raw = Path(cfg.server.log_path).read_text(encoding="utf-8")
    assert not any(s in raw for s in ("fake-tmdb", "secret-server-token", "oat-secret-value"))
    (row,) = [r for r in lines(Path(cfg.server.log_path)) if r["event"] == "tool_call"]
    assert row["question"] == "*** *** ***"


def test_import_netflix_cli(cfg, tmp_path, capsys):
    toml = tmp_path / "lumitv.toml"
    shutil.copy(EXAMPLE, toml)
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            'db_path = "lumitv.db"', f"db_path = {json.dumps((tmp_path / 'cli.db').as_posix())}"
        )
    )
    csv = ROOT / "tests" / "fixtures" / "NetflixViewingHistory.csv"
    assert app_mod.main(["--config", str(toml), "import-netflix", str(csv)]) == 0
    out = capsys.readouterr().out
    n = int(out.split()[1])
    assert n > 0 and len(Store(tmp_path / "cli.db").taste_profile()["most_watched"]) > 0
    assert app_mod.main(["--config", str(toml), "import-netflix", str(csv)]) == 0
    assert "Added 0 rows" in capsys.readouterr().out


def test_uvicorn_config_disables_access_log(cfg):
    uc = app_mod.make_uvicorn_config(make_app(cfg), cfg)
    assert uc.access_log is False


def test_uvicorn_loop_can_run_subprocesses_on_windows(cfg, monkeypatch):
    """Asks start Claude Code as asyncio subprocesses: on Windows only the Proactor loop can do that."""
    import sys

    class Proactor:
        pass

    uc = app_mod.make_uvicorn_config(make_app(cfg), cfg)
    assert uc.loop == "asyncio" and uc.use_subprocess is False  # no reload/workers: those force a Selector loop
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(asyncio, "ProactorEventLoop", Proactor, raising=False)
    assert uc.get_loop_factory() is Proactor


CLIENT_LOGGERS = ("websockets.client", "httpx", "httpcore")  # in-process test client, not the server


async def _tv_connect_logs(cfg) -> list[str]:
    """Serve with real uvicorn, connect to /tv, return the server-side log lines. While connected, uvicorn's own
    logger is also handed a connect line with a token in the URL, as it would log one if a client ever sent it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg.server.host, cfg.server.port = "127.0.0.1", port
    lines: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            if not record.name.startswith(CLIENT_LOGGERS):
                lines.append(record.getMessage())

    uc = app_mod.make_uvicorn_config(make_app(cfg), cfg)  # applies the server's logging config
    grab = Grab(level=logging.DEBUG)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(grab)
    root.setLevel(logging.DEBUG)
    server = uvicorn.Server(uc)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addHandler(grab)  # uvicorn loggers do not propagate
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started
        async with websockets.connect(f"ws://127.0.0.1:{port}/tv", additional_headers=tv_auth()) as ws:
            assert '"hello"' in await ws.recv()
            logging.getLogger("uvicorn.error").info(
                '%s - "WebSocket %s" [accepted]', "('127.0.0.1', 1)", "/tv?token=secret-server-token&x=1"
            )
        await asyncio.sleep(0.1)
    finally:
        server.should_exit = True
        await task
        root.removeHandler(grab)
        root.setLevel(old_level)
        for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
            logging.getLogger(name).removeHandler(grab)
    return lines


async def test_tv_token_never_appears_in_logs(cfg):
    lines = await _tv_connect_logs(cfg)
    assert lines, "expected some server log output"
    assert not any("secret-server-token" in ln for ln in lines)
    assert any("WebSocket /tv" in ln and "token=" not in ln for ln in lines)  # the real connect: header, no token
    assert any('"WebSocket /tv?token=***&x=1" [accepted]' in ln for ln in lines)  # a URL token would be masked


def test_server_log_lines_have_timestamps_and_httpx_is_quiet(cfg):
    """lumitv.* from INFO up reaches stderr with a timestamp, level and logger name, through the token filter; httpx's
    INFO line per request (it quotes the URL, search words included) and other libraries' INFO do not.
    (The dictConfig replaces the root handlers, caplog's among them, for the rest of this test: read the handler.)"""
    app_mod.make_uvicorn_config(make_app(cfg), cfg)  # uvicorn applies the dictConfig
    handler = next(h for h in logging.getLogger().handlers if h.get_name() == "lumitv")
    buf = io.StringIO()
    previous = handler.setStream(buf)
    try:
        logging.getLogger("lumitv.claude").info("claude process started: spare %s", "ab12")
        logging.getLogger("lumitv.metrics").warning("ask_limited token=secret-server-token")
        logging.getLogger("lumitv.tools").debug("not at this level")
        logging.getLogger("httpx").info(
            "HTTP Request: GET https://api.themoviedb.org/3/search/multi?query=the+question"
        )
        logging.getLogger("httpx2").info("HTTP Request: GET https://example.test/")
        logging.getLogger("mcp.server.lowlevel.server").info("Processing request of type CallToolRequest")
        logging.getLogger("some.library").warning("worth a line")
    finally:
        handler.setStream(previous)
    out = buf.getvalue().splitlines()
    stamp = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} "
    assert re.fullmatch(stamp + "INFO lumitv.claude claude process started: spare ab12", out[0])
    assert re.fullmatch(stamp + r"WARNING lumitv.metrics ask_limited token=\*\*\*", out[1])
    assert re.fullmatch(stamp + "WARNING some.library worth a line", out[2])
    assert len(out) == 3


def test_redact_filter_only_rewrites_matching_records():
    f = app_mod.RedactToken()
    plain = logging.LogRecord("x", logging.INFO, "", 0, "hello %s", ("w",), None)
    f.filter(plain)
    assert plain.msg == "hello %s" and plain.args == ("w",)
    hit = logging.LogRecord("x", logging.INFO, "", 0, "WebSocket %s", ("/tv?token=abc&x=1",), None)
    f.filter(hit)
    assert hit.getMessage() == "WebSocket /tv?token=***&x=1"


# ---- POST /ask ----

AUTH = {"Authorization": "Bearer secret-server-token"}


def tv_auth() -> dict:
    """The TV link's header, a fresh dict each time: Starlette's test client adds its handshake headers to it."""
    return dict(AUTH)


ASK = {"ask_id": "abcd1234", "text": "something funny", "source": "typed"}


def test_post_ask_requires_bearer(cfg):
    with TestClient(make_app(cfg)) as c:
        assert c.post("/ask", json=ASK).status_code == 401
        assert c.post("/ask", json=ASK, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_post_ask_validates_body(cfg):
    with TestClient(make_app(cfg)) as c:
        for field, value in (("ask_id", "bad id!"), ("text", "x" * 201), ("source", "shout"), ("focused_tokn", "t")):
            r = c.post("/ask", json={**ASK, field: value}, headers=AUTH)
            assert r.status_code == 400 and r.json()["field"] == field, (field, r.text)
        r = c.post("/ask", content=b"{not json", headers={**AUTH, "Content-Type": "application/json"})
        assert r.status_code == 400
        r = c.post("/ask", content=b"{" + b" " * 9000 + b"}", headers={**AUTH, "Content-Type": "application/json"})
        assert r.status_code == 413  # the body is capped like /ask/audio's
        r = c.post("/ask", json=["a list"], headers=AUTH)
        assert r.status_code == 400


def test_post_ask_returns_202(cfg):
    with TestClient(make_app(cfg)) as c:
        with c.websocket_connect("/tv", headers=tv_auth()) as ws:
            assert ws.receive_json()["type"] == "hello"
            r = c.post("/ask", json={**ASK, "source": "voice"}, headers=AUTH)
            assert r.status_code == 202 and r.json() == {"ask_id": "abcd1234"}
            assert ws.receive_json() == {"type": "heard", "ask_id": "abcd1234", "text": "something funny"}
            # no Claude Code on this machine (claude_path points nowhere): the TV hears why at once
            assert ws.receive_json() == {"type": "ask_error", "ask_id": "abcd1234", "code": "not_installed"}
            # and /stats keeps it for `lumitv doctor`
            last = c.get("/stats", headers=AUTH).json()["ask"]["last"]
            assert last["outcome"] == "not_installed" and 0 <= last["age_s"] < 60
    assert (cfg.ask.workdir).is_dir()


def test_max_question_chars_from_config(cfg):
    cfg.ask.max_question_chars = 20
    with TestClient(make_app(cfg)) as c:
        r = c.post("/ask", json={**ASK, "text": "x" * 21}, headers=AUTH)
        assert r.status_code == 400 and r.json() == {"error": "invalid", "field": "text"}
        assert c.post("/ask", json={**ASK, "text": "x" * 20}, headers=AUTH).status_code == 202


def test_stats_ask_last_is_null_before_any_ask(cfg):
    with TestClient(make_app(cfg)) as c:
        assert c.get("/stats", headers=AUTH).json()["ask"]["last"] is None


def test_stats_is_read_only(cfg):
    """`/stats` answers GET only. Another method falls through to the catch-all mount (the MCP app, behind the same
    token), which knows no such path: the stats are not served either way."""
    with TestClient(make_app(cfg)) as c:
        assert c.get("/stats", headers=AUTH).status_code == 200
        for method in ("post", "put", "delete"):
            r = getattr(c, method)("/stats", headers=AUTH)
            assert r.status_code in (404, 405) and "cache_hit_rate" not in r.text, (method, r.status_code)


def test_hello_says_whether_the_server_hears_speech(cfg):
    assert cfg.speech.engine == "none"
    with TestClient(make_app(cfg)) as c, c.websocket_connect("/tv", headers=tv_auth()) as ws:
        assert ws.receive_json()["speech"] is False
    with TestClient(audio_app(cfg)) as c, c.websocket_connect("/tv", headers=tv_auth()) as ws:
        assert ws.receive_json()["speech"] is True
    cfg.ask.enabled = False  # no asks at all: nothing to send audio to
    with TestClient(audio_app(cfg)) as c, c.websocket_connect("/tv", headers=tv_auth()) as ws:
        assert ws.receive_json()["speech"] is False


def test_hello_speech_false_once_the_model_failed_to_load(cfg, monkeypatch):
    from lumitv import stt as stt_mod

    def broken(_speech):
        raise OSError("no model")

    monkeypatch.setattr(stt_mod, "load_model", broken)
    whisper = stt_mod.WhisperTranscriber(cfg.speech)
    with TestClient(audio_app(cfg, whisper)) as c:
        c.portal.call(lambda: asyncio.gather(whisper._loading, return_exceptions=True))
        with c.websocket_connect("/tv", headers=tv_auth()) as ws:
            assert ws.receive_json()["speech"] is False


def test_hello_speech_turns_on_once_the_model_is_loaded(cfg, monkeypatch):
    """While the model loads (10-25 s after every start) hello says speech: false, so the TV's own recognizer answers
    instead of the first spoken ask failing; a TV connected meanwhile is told again when the model is in."""
    import threading

    from lumitv import stt as stt_mod

    gate = threading.Event()

    def slow_load(_speech):
        gate.wait(10)
        return object()  # the model

    monkeypatch.setattr(stt_mod, "load_model", slow_load)
    whisper = stt_mod.WhisperTranscriber(cfg.speech)
    with TestClient(audio_app(cfg, whisper)) as c, c.websocket_connect("/tv", headers=tv_auth()) as ws:
        assert ws.receive_json()["speech"] is False
        gate.set()
        again = ws.receive_json()
        assert (
            again["type"] == "hello" and again["speech"] is True and again["strings"]
        )  # the full hello, as on connect
        with c.websocket_connect("/tv", headers=tv_auth()) as later:
            assert later.receive_json()["speech"] is True


def test_ask_disabled_returns_503(cfg):
    cfg.ask.enabled = False
    with TestClient(make_app(cfg)) as c:
        r = c.post("/ask", json=ASK, headers=AUTH)
        assert r.status_code == 503 and r.json() == {"error": "disabled"}
    assert not cfg.ask.workdir.exists()


def test_tool_calls_from_a_claude_process_carry_its_ask(cfg, monkeypatch):
    """The app maps a tool call's X-Lumi-Process header to the ask that process answers (ClaudeRunner.ask_id_of)."""
    from lumitv.claude_runner import ClaudeRunner

    monkeypatch.setattr(
        ClaudeRunner,
        "ask_id_of",
        lambda self, headers: "abcd1234" if headers and headers.get("x-lumi-process") else None,
    )
    with TestClient(make_app(cfg)) as c:
        with c.websocket_connect("/tv", headers=tv_auth()) as ws:
            assert ws.receive_json()["type"] == "hello"
            h = {**HEADERS, **AUTH}
            h["mcp-session-id"] = c.post("/mcp", json=INIT, headers=h).headers["mcp-session-id"]
            c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
            call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "find_titles", "arguments": {"question": "q"}}}  # fmt: skip
            c.post("/mcp", json=call, headers={**h, "X-Lumi-Process": "p1"})
            looking = ws.receive_json()
            assert looking["type"] == "looking" and looking["ask_id"] == "abcd1234"


def test_an_ask_runs_through_the_app_into_a_claude_process(cfg, tmp_path, monkeypatch):
    """The headline path, wired for real: POST /ask -> AskService -> ClaudeRunner starts the CLI at [ask] claude_path
    (a launcher for the fake here) with this server's MCP config -> its answer reaches the TV as ask_reply."""
    import sys

    from lumitv import claude_runner

    fake = ROOT / "tests" / "fixtures" / "fake_claude.py"
    out = tmp_path / "fake-out"
    out.mkdir()
    monkeypatch.setenv("FAKE_CLAUDE_OUT", str(out))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setattr(
        claude_runner, "CHILD_ENV_KEEP_PREFIXES", (*claude_runner.CHILD_ENV_KEEP_PREFIXES, "FAKE_CLAUDE_")
    )
    if sys.platform == "win32":
        launcher = tmp_path / "claude.cmd"
        launcher.write_text(f'@"{sys.executable}" "{fake}" %*\r\n')
    else:
        launcher = tmp_path / "claude"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
        launcher.chmod(0o755)
    cfg.ask.claude_path = str(launcher)
    with TestClient(make_app(cfg)) as c, c.websocket_connect("/tv", headers=tv_auth()) as ws:
        ws.receive_json()
        assert c.post("/ask", json=ASK, headers=AUTH).status_code == 202
        assert ws.receive_json() == {"type": "heard", "ask_id": "abcd1234", "text": "something funny"}
        assert ws.receive_json() == {"type": "ask_reply", "ask_id": "abcd1234", "text": "Here are three"}
        for _ in range(100):
            last = c.get("/stats", headers=AUTH).json()["ask"]["last"]
            if last and last["outcome"] == "reply":
                break
            time.sleep(0.02)
        assert last["outcome"] == "reply"
    records = [json.loads(p.read_text(encoding="utf-8")) for p in out.glob("*.json")]
    (served,) = [r for r in records if r["turns"]]  # the pre-warmed process took the ask; a new spare followed it
    assert served["turns"] == ["The user typed: something funny"]
    lumi = served["mcp"]["mcpServers"]["lumi"]
    assert (
        lumi["url"] == f"http://127.0.0.1:{cfg.server.port}/mcp"
        and lumi["headers"]["Authorization"] == "Bearer secret-server-token"
    )
    prompt = served["argv"][served["argv"].index("--append-system-prompt") + 1]
    assert prompt.startswith("You are Lumi, helping a family")
    if (
        os.name != "nt"
    ):  # the .cmd shim that stands in for the CLI cuts an argument at its first newline; claude.exe does not
        assert "exactly two tool calls" in prompt


async def test_shutdown_stops_claude_before_waiting_for_connections(cfg, monkeypatch):
    """Warm Claude Code processes keep MCP connections open; uvicorn waits for open connections before the lifespan
    shutdown, so the server must stop Claude first or it never exits."""
    app = make_app(cfg)
    order = []

    async def close_asks():
        order.append("claude stopped")

    async def uvicorn_shutdown(self, sockets=None):
        order.append("uvicorn waits for connections")

    app.state.close_asks = close_asks
    monkeypatch.setattr(uvicorn.Server, "shutdown", uvicorn_shutdown)
    await app_mod.LumiServer(app_mod.make_uvicorn_config(app, cfg)).shutdown()
    assert order == ["claude stopped", "uvicorn waits for connections"]


# --- POST /ask/audio ------------------------------------------------------------------------------------------


class FakeTranscriber:
    def __init__(self, text="something like the expanse"):
        self.text = text
        self.got = []

    async def transcribe(self, audio):
        self.got.append(audio)
        return self.text


def wav(seconds=1.0, **kw):
    from test_stt import make_wav

    return make_wav(seconds, **kw)


WAV = {"Content-Type": "audio/wav"}
AUDIO_URL = "/ask/audio?ask_id=abcd1234"


def audio_app(cfg, transcriber=None):
    return app_mod.create_app(cfg, source=FakeSource({1: det("One")}), transcriber=transcriber or FakeTranscriber())


def test_audio_endpoint_requires_bearer(cfg):
    with TestClient(audio_app(cfg)) as c:
        assert c.post(AUDIO_URL, content=wav(), headers=WAV).status_code == 401
        assert c.post(AUDIO_URL, content=wav(), headers={**WAV, "Authorization": "Bearer nope"}).status_code == 401


def test_audio_endpoint_returns_202_then_heard(cfg):
    stt = FakeTranscriber()
    with TestClient(audio_app(cfg, stt)) as c:
        with c.websocket_connect("/tv", headers=tv_auth()) as ws:
            assert ws.receive_json()["type"] == "hello"
            r = c.post(AUDIO_URL + "&answer_id=a1&focused_token=t1", content=wav(), headers={**WAV, **AUTH})
            assert r.status_code == 202 and r.json() == {"ask_id": "abcd1234"}
            assert ws.receive_json() == {"type": "heard", "ask_id": "abcd1234", "text": "something like the expanse"}
            assert ws.receive_json() == {
                "type": "ask_error",
                "ask_id": "abcd1234",
                "code": "not_installed",
            }  # no Claude Code
    assert len(stt.got) == 1


def test_audio_endpoint_noise_is_not_heard(cfg):
    """[speech] ignore_phrases reach the ask service: "Thank you." has letters enough, only the phrase list stops it."""
    with TestClient(audio_app(cfg, FakeTranscriber("Thank you."))) as c:
        with c.websocket_connect("/tv", headers=tv_auth()) as ws:
            assert ws.receive_json()["type"] == "hello"
            assert c.post(AUDIO_URL, content=wav(), headers={**WAV, **AUTH}).status_code == 202
            assert ws.receive_json() == {"type": "ask_error", "ask_id": "abcd1234", "code": "not_heard"}


def test_audio_endpoint_too_large_413(cfg):
    with TestClient(audio_app(cfg)) as c:
        r = c.post(AUDIO_URL, content=b"\0" * (1024 * 1024 + 1), headers={**WAV, **AUTH})
        assert r.status_code == 413
        r = c.post(AUDIO_URL, content=wav(16), headers={**WAV, **AUTH})  # under 1 MB, but over 15 s
        assert r.status_code == 413


def test_audio_endpoint_bad_format_415(cfg):
    with TestClient(audio_app(cfg)) as c:
        r = c.post(AUDIO_URL, content=wav(rate=44100, channels=2), headers={**WAV, **AUTH})
        assert r.status_code == 415
        r = c.post(AUDIO_URL, content=b"not a wav", headers={**WAV, **AUTH})
        assert r.status_code == 415
        r = c.post(AUDIO_URL, content=wav(), headers={"Content-Type": "application/octet-stream", **AUTH})
        assert r.status_code == 415


def test_audio_endpoint_bad_ask_id_400(cfg):
    with TestClient(audio_app(cfg)) as c:
        for url in ("/ask/audio", "/ask/audio?ask_id=bad%20id!"):
            r = c.post(url, content=wav(), headers={**WAV, **AUTH})
            assert r.status_code == 400 and r.json() == {"error": "invalid", "field": "ask_id"}, url


def test_audio_endpoint_engine_none_503(cfg):
    assert cfg.speech.engine == "none"  # the example config
    with TestClient(make_app(cfg)) as c:
        r = c.post(AUDIO_URL, content=wav(), headers={**WAV, **AUTH})
        assert r.status_code == 503 and r.json() == {"error": "no speech-to-text"}


def test_audio_endpoint_asks_disabled_503(cfg):
    cfg.ask.enabled = False
    with TestClient(audio_app(cfg)) as c:
        r = c.post(AUDIO_URL, content=wav(), headers={**WAV, **AUTH})
        assert r.status_code == 503 and r.json() == {"error": "disabled"}


async def test_audio_endpoint_client_gone_mid_upload_is_dropped_quietly(cfg):
    app = audio_app(cfg)
    scope = {
        "type": "http", "method": "POST", "path": "/ask/audio", "raw_path": b"/ask/audio", "root_path": "",
        "query_string": b"ask_id=abcd1234", "scheme": "http", "server": ("t", 80), "client": ("c", 1),
        "http_version": "1.1",
        "headers": [(b"authorization", b"Bearer secret-server-token"), (b"content-type", b"audio/wav")],
    }  # fmt: skip
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    assert sent == []


def test_asks_past_the_per_minute_cap_get_429_limit(cfg):
    cfg.ask.per_minute = 2
    with TestClient(make_app(cfg)) as c:
        codes = [
            c.post("/ask", json={"ask_id": f"abcd123{i}", "text": "funny", "source": "typed"}, headers=AUTH).status_code
            for i in range(3)
        ]
    assert codes == [202, 202, 429]


def test_spoken_asks_are_capped_before_their_audio_is_read(cfg):
    cfg.ask.per_minute = 1
    with TestClient(audio_app(cfg)) as c:
        refused = c.post(AUDIO_URL, content=b"not a wav", headers={**WAV, **AUTH})
        first = c.post("/ask/audio?ask_id=abcd0001", content=wav(), headers={**WAV, **AUTH})
        second = c.post("/ask/audio?ask_id=abcd0002", content=wav(), headers={**WAV, **AUTH})
    assert refused.status_code == 415  # a refused clip costs no quota ...
    assert first.status_code == 202  # ... so this one still fits
    assert second.status_code == 429 and second.json() == {"error": "limit", "cap": "per_minute"}


def test_per_day_cap_returns_429_and_resets_at_midnight(cfg):
    from datetime import date

    from lumitv.budget import AskBudget
    from test_metrics import lines

    cfg.ask.per_day = 1
    day = [date(2026, 11, 4)]
    budget = AskBudget(cfg.ask.per_minute, cfg.ask.per_day, today=lambda: day[0])
    with TestClient(app_mod.create_app(cfg, source=FakeSource({1: det("One")}), budget=budget)) as c:
        assert c.post("/ask", json={**ASK, "ask_id": "day00001"}, headers=AUTH).status_code == 202
        r = c.post("/ask", json={**ASK, "ask_id": "day00002"}, headers=AUTH)
        assert r.status_code == 429 and r.json() == {"error": "limit", "cap": "per_day"}
        day[0] = date(2026, 11, 5)
        assert c.post("/ask", json={**ASK, "ask_id": "day00003"}, headers=AUTH).status_code == 202
    (row,) = [r for r in lines(Path(cfg.server.log_path)) if r["event"] == "ask_limited"]
    assert (row["level"], row["cap"], row["used_today"]) == ("WARNING", "per_day", 1)


def test_asks_that_never_run_cost_no_quota(cfg):
    """The budget counts Claude runs: a repeated ask_id (accepted, ignored) and a refused ask leave it alone."""
    cfg.ask.per_day = 2
    with TestClient(make_app(cfg)) as c:

        def post(ask_id, text="funny"):
            return c.post("/ask", json={**ASK, "ask_id": ask_id, "text": text}, headers=AUTH)

        assert post("aaaa0001").status_code == 202  # runs: 1 of 2
        assert post("aaaa0001").status_code == 202  # the same ask again: ignored, not counted
        assert post("aaaa0002", "x" * 201).status_code == 400  # refused: not counted
        assert post("aaaa0003").status_code == 202  # runs: 2 of 2
        r = post("aaaa0004")
        assert r.status_code == 429 and r.json()["cap"] == "per_day"


class WarmTranscriber(FakeTranscriber):
    def __init__(self):
        super().__init__()
        self.warms = 0

    async def warm(self):
        self.warms += 1
        return 0.25


def test_mic_open_warms_the_speech_model_and_logs_it(cfg):
    from test_metrics import lines

    stt = WarmTranscriber()
    with TestClient(audio_app(cfg, stt)) as c, c.websocket_connect("/tv", headers=tv_auth()) as ws:
        ws.receive_json()
        ws.send_json({"type": "ask_warm", "ask_id": "abcd1234"})
        for _ in range(100):
            if stt.warms and "stt_warm" in Path(cfg.server.log_path).read_text(encoding="utf-8"):
                break
            time.sleep(0.02)
    (row,) = [r for r in lines(Path(cfg.server.log_path)) if r["event"] == "stt_warm"]
    assert stt.warms == 1 and row["ask_id"] == "abcd1234" and row["ms"] == 250


def test_startup_warns_when_the_wikidata_contact_is_missing(cfg):
    from test_metrics import lines

    cfg.open.user_agent_contact = ""
    with TestClient(make_app(cfg)):
        pass
    (row,) = [r for r in lines(Path(cfg.server.log_path)) if r["event"] == "setup_incomplete"]
    assert row["missing"] == "[open] user_agent_contact"


# --- the household: viewer names ------------------------------------------------------------------------------


def test_viewer_name_rules():
    """Names are trimmed and bounded, and never contain the characters they travel with (`,` in ?viewers=, `\\n` in
    the store), so a name round-trips through both."""
    from pydantic import ValidationError

    from lumitv.models import MAX_VIEWER_NAME, Viewer

    assert Viewer(name="  Emma  ", age=6).name == "Emma"
    assert Viewer(name="x" * MAX_VIEWER_NAME).name == "x" * MAX_VIEWER_NAME
    for bad in ("", "   ", "x" * (MAX_VIEWER_NAME + 1), "Emma, Jr", "Em\nma", "Em\rma"):
        with pytest.raises(ValidationError):
            Viewer(name=bad)


def test_viewers_set_is_validated_and_clamped(cfg):
    """A bad entry refuses the whole list and echoes the current one; a long list keeps the first MAX_VIEWERS."""
    from lumitv.models import MAX_VIEWERS

    with TestClient(make_app(cfg)) as c, c.websocket_connect("/tv", headers=tv_auth()) as ws:
        assert ws.receive_json()["viewers"] == []
        ws.send_json({"type": "viewers_set", "viewers": [{"name": "Emma", "age": 6}]})
        assert ws.receive_json() == {"type": "viewers", "viewers": [{"name": "Emma", "age": 6}]}
        ws.send_json({"type": "viewers_set", "viewers": [{"name": "Noah", "age": 200}]})  # not a plausible age
        assert ws.receive_json() == {"type": "viewers", "viewers": [{"name": "Emma", "age": 6}]}
        ws.send_json({"type": "viewers_set", "viewers": [{"name": "Emma, Jr", "age": 6}]})  # can't travel in ?viewers=
        assert ws.receive_json() == {"type": "viewers", "viewers": [{"name": "Emma", "age": 6}]}
        ws.send_json({"type": "viewers_set", "viewers": [{"name": f"P{i}"} for i in range(MAX_VIEWERS + 1)]})
        assert [v["name"] for v in ws.receive_json()["viewers"]] == [f"P{i}" for i in range(MAX_VIEWERS)]
        ws.send_json({"type": "viewers_set", "viewers": [{"name": "  Alex  "}]})
        assert ws.receive_json()["viewers"] == [{"name": "Alex", "age": None}]
    assert [v.name for v in Store(cfg.server.db_path).viewers()] == ["Alex"]
    with TestClient(make_app(cfg)) as c, c.websocket_connect("/tv", headers=tv_auth()) as ws:
        assert ws.receive_json()["viewers"] == [{"name": "Alex", "age": None}]  # hello carries the saved list


def test_spoken_ask_viewer_names_are_trimmed_and_unknown_ones_counted(cfg):
    """`?viewers=Noah, Emma` names Emma (the space is not part of her name); Noah is counted as unknown in the log."""
    from lumitv.models import Viewer
    from test_metrics import lines

    Store(cfg.server.db_path).set_viewers([Viewer(name="Emma", age=6)])
    with TestClient(audio_app(cfg)) as c, c.websocket_connect("/tv", headers=tv_auth()) as ws:
        ws.receive_json()
        r = c.post(AUDIO_URL + "&viewers=Noah,%20Emma%20", content=wav(), headers={**WAV, **AUTH})
        assert r.status_code == 202
        assert ws.receive_json()["type"] == "heard"
        assert ws.receive_json()["code"] == "not_installed"
        for _ in range(100):
            if '"event": "ask"' in Path(cfg.server.log_path).read_text(encoding="utf-8"):
                break
            time.sleep(0.02)
    (row,) = [r for r in lines(Path(cfg.server.log_path)) if r["event"] == "ask"]
    assert row["viewers_unknown"] == 1
