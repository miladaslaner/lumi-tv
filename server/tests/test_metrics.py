import json
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from lumitv import app as app_mod, doctor as doctor_mod
from lumitv.metrics import ASK_STAGES, Metrics
from test_app import HEADERS, INIT, cfg, make_app  # noqa: F401  (cfg fixture)
from test_tools import Rig, data, det


def lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_stats_percentiles(tmp_path):
    m = Metrics(tmp_path / "l.jsonl")
    for i, ms in enumerate([10, 20, 30, 40, 50, 60, 70, 80, 90, 100]):
        m.record_turn(f"t{i}", "find", ms)
    s = m.stats()["find"]
    assert (s["p50"], s["p95"], s["count"]) == (50, 100, 10)  # nearest rank


def test_stats_window_is_last_50_turns(tmp_path):
    m = Metrics(tmp_path / "l.jsonl")
    for i in range(60):
        m.record_turn(f"t{i}", "find", i)
    assert m.stats()["find"]["count"] == 50
    assert m.stats()["find"]["p50"] == 34  # turns 10..59


def test_cache_hit_rate(tmp_path):
    m = Metrics(tmp_path / "l.jsonl")
    assert m.stats()["cache_hit_rate"] is None
    with m.track_call():
        for hit in (True, True, True, False):
            m.cache(hit)
    assert m.stats()["cache_hit_rate"] == 0.75


def test_log_redacts_token(tmp_path):
    path = tmp_path / "l.jsonl"
    m = Metrics(path, secrets=["s3cret-value"])
    m.log("x", token="s3cret-value", Authorization="Bearer s3cret-value", nested={"Token": "a", "ok": 1},
          note="oops s3cret-value leaked", ms=3)  # fmt: skip
    raw = path.read_text(encoding="utf-8")
    assert "s3cret-value" not in raw and "Bearer" not in raw
    row = json.loads(raw)
    assert row["event"] == "x" and row["ms"] == 3 and row["nested"] == {"ok": 1} and "ts" in row
    assert "token" not in row and "Authorization" not in row


async def test_every_tool_call_logged_with_duration(tmp_path):
    rig = Rig(
        tmp_path, {1: det("One"), 2: det("Two")}, metrics=Metrics(tmp_path / "l.jsonl", secrets=["tok-secret-123"])
    )
    found = data(await rig.call("find_titles", question="something fun"))
    tokens = [t["token"] for t in found["titles"]]
    await rig.call("show_on_tv", headline="h", subline="s", tokens=tokens[:1])
    rows = [r for r in lines(tmp_path / "l.jsonl") if r["event"] == "tool_call"]
    assert [r["tool"] for r in rows] == ["find_titles", "show_on_tv"]
    for r in rows:
        assert r["status"] == "ok" and r["ms"] >= 0 and r["turn_id"]
    assert rows[0]["turn_id"] == rows[1]["turn_id"]
    assert rows[0]["count"] == 2 and rows[0]["inputs_failed"] == 0 and rows[0]["details_failed"] == 0
    assert rows[0]["question"] == "something fun"
    assert "tok-secret-123" not in (tmp_path / "l.jsonl").read_text(encoding="utf-8")
    assert all("token" not in r for r in rows)
    stats = rig.metrics.stats()
    assert stats["find"]["count"] == 1 and stats["show"]["count"] == 1


async def test_failed_tool_call_logged_as_error(tmp_path):
    rig = Rig(tmp_path, {1: det("One")}, metrics=Metrics(tmp_path / "l.jsonl"))
    await rig.call("show_on_tv", headline="h", subline="s", tokens=["nope"])
    (row,) = [r for r in lines(tmp_path / "l.jsonl") if r["event"] == "tool_call"]
    assert row["status"] == "error" and row["tool"] == "show_on_tv"


def test_stats_requires_bearer(cfg):  # noqa: F811
    with TestClient(make_app(cfg)) as c:
        assert c.get("/stats").status_code == 401
        r = c.get("/stats", headers={"Authorization": "Bearer secret-server-token"})
        assert r.status_code == 200
        body = r.json()
        assert body["find"]["count"] == 0 and "cache_hit_rate" in body and body["tv"]["connected"] is False


def _drive_tv(cfg, with_state=True):  # noqa: F811
    """Connect a fake TV, find + show one title, ack and report render. Returns /stats body."""
    import threading

    auth = {**HEADERS, "Authorization": "Bearer secret-server-token"}
    with TestClient(make_app(cfg)) as c:
        with c.websocket_connect("/tv", headers={"Authorization": "Bearer secret-server-token"}) as ws:
            assert ws.receive_json()["type"] == "hello"
            if with_state:
                ws.send_json({"type": "state", "screen_on": True, "playing": False, "vpn": True})
                ws.send_json({"type": "state", "screen_on": True, "playing": False, "vpn": True})  # no change
            auth["mcp-session-id"] = c.post("/mcp", json=INIT, headers=auth).headers["mcp-session-id"]
            c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=auth)

            def call(i, name, args):
                r = c.post("/mcp", json={"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                         "params": {"name": name, "arguments": args}}, headers=auth)  # fmt: skip
                return r

            found = call(2, "find_titles", {"question": "q"})
            line = [x for x in found.text.splitlines() if x.startswith("data: ")][-1][6:]
            token = json.loads(json.loads(line)["result"]["content"][0]["text"])["titles"][0]["token"]
            t = threading.Thread(
                target=call, args=(3, "show_on_tv", {"headline": "h", "subline": "s", "tokens": [token]})
            )
            t.start()
            shown = ws.receive_json()
            while shown["type"] == "looking":
                shown = ws.receive_json()
            assert shown["type"] == "show"
            ws.send_json({"type": "shown", "answer_id": shown["answer_id"], "mode": "screen"})
            t.join(5)
            ws.send_json(
                {
                    "type": "rendered",
                    "answer_id": shown["answer_id"],
                    "ms": 123,
                    "phases": {"created": 80, "frame": 100},
                }
            )
            ws.send_json({"type": "opened", "token": token})
            import time

            for _ in range(100):
                body = c.get("/stats", headers=auth).json()
                if body["render"]["count"] and Path(cfg.server.log_path).read_text(encoding="utf-8").count("tv_opened"):
                    break
                time.sleep(0.05)
    return body


def test_rendered_event_recorded_against_turn(cfg):  # noqa: F811
    body = _drive_tv(cfg)
    assert body["render"]["count"] == 1 and body["render"]["p50"] == 123
    assert body["show"]["count"] == 1 and body["find"]["count"] == 1
    rows = lines(Path(cfg.server.log_path))
    (rendered,) = [r for r in rows if r["event"] == "tv_rendered"]
    find = next(r for r in rows if r["event"] == "tool_call" and r["tool"] == "find_titles")
    assert rendered["turn_id"] == find["turn_id"] and rendered["ms"] == 123
    assert rendered["created_ms"] == 80 and rendered["frame_ms"] == 100 and "start_ms" not in rendered


def test_tv_events_logged_without_tokens(cfg):  # noqa: F811
    _drive_tv(cfg)
    rows = lines(Path(cfg.server.log_path))
    events = [r["event"] for r in rows if r["event"].startswith("tv_")]
    assert events[0] == "tv_connected" and events.count("tv_state") == 1 and "tv_opened" in events
    assert events[-1] == "tv_disconnected"
    state = next(r for r in rows if r["event"] == "tv_state")
    assert (state["screen_on"], state["playing"], state["vpn"]) == (True, False, True)
    raw = Path(cfg.server.log_path).read_text(encoding="utf-8")
    assert "secret-server-token" not in raw and "fake-tmdb" not in raw


async def test_warm_traffic_does_not_touch_live_cache_counts(tmp_path):
    import asyncio

    m = Metrics(tmp_path / "l.jsonl")

    async def warm_like():
        for _ in range(200):
            m.cache(False)
            await asyncio.sleep(0)

    background = asyncio.create_task(warm_like())  # created outside any call, like the warm task
    with m.track_call() as counts:
        for _ in range(5):
            m.cache(True)
            await asyncio.sleep(0)
        m.cache(False)
    await background
    assert counts == {"hits": 5, "misses": 1}
    assert m.stats()["cache_hit_rate"] == 5 / 6


async def test_hit_rate_window_is_bounded(tmp_path):
    from lumitv.metrics import CACHE_WINDOW

    m = Metrics(tmp_path / "l.jsonl")
    with m.track_call():
        for _ in range(CACHE_WINDOW):
            m.cache(False)
        for _ in range(CACHE_WINDOW):
            m.cache(True)
    assert m.stats()["cache_hit_rate"] == 1.0


async def test_concurrent_calls_have_separate_cache_counts(tmp_path):
    import asyncio

    m = Metrics(tmp_path / "l.jsonl")

    async def call(n):
        with m.track_call() as counts:
            for _ in range(n):
                m.cache(True)
                await asyncio.sleep(0)
        return counts["hits"]

    assert await asyncio.gather(call(3), call(7)) == [3, 7]


# ---- doctor ----

BUDGET = None


def stats_body(**over):
    base = {
        "find": {"p50": 100, "p95": 200, "count": 5},
        "show": {"p50": 50, "p95": 80, "count": 5},
        "render": {"p50": 200, "p95": 300, "count": 5},
        "cache_hit_rate": 0.95,
        "turns": 5,
        "tv": {"connected": True, "screen_on": True, "playing": False, "vpn": True},
        "cache_rows": 120,
    }
    base.update(over)
    return base


def run_doctor(cfg, stats, tmdb_status=200, claude="2.1.317 (Claude Code)"):  # noqa: F811
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.themoviedb.org":
            assert req.headers["authorization"] == f"Bearer {cfg.tmdb_token}"
            return httpx.Response(tmdb_status, json={"success": tmdb_status == 200})
        if req.url.path == "/health":
            return httpx.Response(200, text="ok")
        assert req.headers["authorization"] == f"Bearer {cfg.server_token}"
        return httpx.Response(200, json=stats)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    lines_, ok = doctor_mod.run_checks(cfg, client, claude_version=lambda path: claude)
    return "\n".join(lines_), ok


def test_doctor_pass_and_no_secrets(cfg):  # noqa: F811
    out, ok = run_doctor(cfg, stats_body())
    assert ok
    assert "PASS" in out and "FAIL" not in out
    assert "120" in out  # cache rows
    assert cfg.tmdb_token not in out and cfg.server_token not in out


def test_doctor_fails_over_budget(cfg):  # noqa: F811
    slow = stats_body(find={"p50": 100, "p95": cfg.budgets.find_p95_cold_ms + 1, "count": 5})
    out, ok = run_doctor(cfg, slow)
    assert not ok and "FAIL" in out


def test_doctor_bad_tmdb_token(cfg):  # noqa: F811
    out, ok = run_doctor(cfg, stats_body(), tmdb_status=401)
    assert not ok and "TMDB" in out and "FAIL" in out
    assert cfg.tmdb_token not in out


def test_doctor_server_down(cfg):  # noqa: F811
    def handler(req):
        if req.url.host == "api.themoviedb.org":
            return httpx.Response(200, json={})
        raise httpx.ConnectError("refused")

    lines_, ok = doctor_mod.run_checks(cfg, httpx.Client(transport=httpx.MockTransport(handler)))
    assert not ok and any("server" in x.lower() and "FAIL" in x for x in lines_)


def test_doctor_non_json_or_incomplete_stats_is_fail_not_crash(cfg):  # noqa: F811
    def run(body):
        def handler(req):
            if req.url.host == "api.themoviedb.org":
                return httpx.Response(200, json={})
            if req.url.path == "/health":
                return httpx.Response(200, text="ok")
            return httpx.Response(200, **body)

        return doctor_mod.run_checks(cfg, httpx.Client(transport=httpx.MockTransport(handler)))

    for body in ({"text": "<html>"}, {"json": {"find": {}}}, {"json": [1]}):
        lines_, ok = run(body)
        assert not ok and any("FAIL" in x and "stats" in x.lower() for x in lines_)


def test_stale_warning_rate_limited(cfg, monkeypatch):  # noqa: F811
    # one warning per path per minute
    from lumitv import tmdb as tmdb_mod

    clock = [1000.0]
    monkeypatch.setattr(app_mod.time, "monotonic", lambda: clock[0])
    captured = {}
    real = tmdb_mod.Tmdb.__init__

    def spy(self, *a, **kw):
        captured["on_stale"] = kw["on_stale"]
        real(self, *a, **kw)

    monkeypatch.setattr(tmdb_mod.Tmdb, "__init__", spy)
    app_mod.create_app(cfg)
    for _ in range(3):
        captured["on_stale"]("/movie/1", "boom")
    captured["on_stale"]("/movie/2", "boom")
    clock[0] += 61
    captured["on_stale"]("/movie/1", "boom")
    rows = [r for r in lines(Path(cfg.server.log_path)) if r["event"] == "tmdb_stale"]
    assert [r["path"] for r in rows] == ["/movie/1", "/movie/2", "/movie/1"]


def test_ask_stage_percentiles_in_stats(tmp_path):
    m = Metrics(None)
    for i, ms in enumerate([10, 20, 30, 40, 50, 60, 70, 80, 90, 100]):
        m.record_ask(f"a{i}", "stt", ms)
    ask = m.stats()["ask"]
    assert ask["stt"] == {"p50": 50, "p95": 100, "count": 10}
    assert set(ask) == set(ASK_STAGES)
    for stage in ASK_STAGES:
        if stage != "stt":
            assert ask[stage] == {"p50": None, "p95": None, "count": 0}
    assert m.stats()["find"]["count"] == 0  # v1 stages untouched


def test_ask_window_keeps_last_50_asks(tmp_path):
    m = Metrics(None)
    for i in range(60):
        m.record_ask(f"a{i}", "claude_total", i)
    s = m.stats()["ask"]["claude_total"]
    assert s["count"] == 50 and s["p50"] == 34  # asks 10..59


def ask_stats(**over):
    base = {s: {"p50": None, "p95": None, "count": 0} for s in ("listen", "heard", "posters")}
    base.update(over)
    return base


def test_doctor_reports_ask_budgets(cfg):  # noqa: F811
    good = ask_stats(listen={"p50": 600, "p95": 900, "count": 3}, heard={"p50": 2000, "p95": 2900, "count": 3},
                     posters={"p50": 9000, "p95": 11000, "count": 3})  # fmt: skip
    out, ok = run_doctor(cfg, stats_body(ask=good))
    assert ok and "listening on screen p50" in out and "PASS" in out
    slow = ask_stats(posters={"p50": 13000, "p95": 15000, "count": 3})
    out, ok = run_doctor(cfg, stats_body(ask=slow))
    assert not ok
    fail = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
    assert len(fail) == 1 and "12000" in fail[0] and "13000" in fail[0]


def test_doctor_reports_claude_code(cfg):  # noqa: F811
    out, ok = run_doctor(cfg, stats_body())
    assert ok and "INFO  Claude Code (2.1.317 (Claude Code))" in out
    out, ok = run_doctor(cfg, stats_body(), claude=None)
    assert not ok and "FAIL  Claude Code" in out


def test_doctor_skips_claude_code_when_asks_disabled(cfg):  # noqa: F811
    cfg.ask.enabled = False
    out, ok = run_doctor(cfg, stats_body(), claude=None)
    assert ok and "Claude Code" not in out


def test_claude_version_of_missing_binary_is_none(tmp_path):
    assert doctor_mod.claude_version(str(tmp_path / "nope")) is None


@pytest.mark.parametrize(
    "outcome, hint", [("login", "/login"), ("limit", "usage limit"), ("not_installed", "claude_path")]
)
def test_doctor_fails_on_a_recent_ask_needing_the_operator(cfg, outcome, hint):  # noqa: F811
    out, ok = run_doctor(cfg, stats_body(ask={"last": {"outcome": outcome, "age_s": 120}}))
    assert not ok
    line = next(x for x in out.splitlines() if "Last ask" in x)
    assert line.startswith("FAIL") and outcome in line and hint in line


def test_doctor_last_ask_ok_or_old_is_info(cfg):  # noqa: F811
    for last in ({"outcome": "shown", "age_s": 5}, {"outcome": "login", "age_s": doctor_mod.RECENT_ASK_S + 1}):
        out, ok = run_doctor(cfg, stats_body(ask={"last": last}))
        assert ok, out
        assert next(x for x in out.splitlines() if "Last ask" in x).startswith("INFO")
    out, ok = run_doctor(cfg, stats_body(ask={"last": None}))  # no ask yet
    assert ok and "Last ask" not in out


def test_doctor_fails_when_the_claude_cli_is_missing(cfg):  # noqa: F811
    out, ok = run_doctor(cfg, stats_body(), claude=None)
    assert not ok and any(x.startswith("FAIL") and "Claude Code" in x for x in out.splitlines())


def test_doctor_needs_a_wikidata_contact_when_a_service_opens_exact_titles(cfg):  # noqa: F811
    # the example config opens Netflix by {netflix_id} and Prime via Google TV: both ids come from Wikidata
    cfg.open.user_agent_contact = ""  # as in lumitv.example.toml
    line, ok = doctor_mod.contact_check(cfg)
    assert ok is False and "user_agent_contact" in line and "FAIL" in line
    cfg.open.user_agent_contact = "someone@example.org"
    line, ok = doctor_mod.contact_check(cfg)
    assert ok is True and "someone@example.org" not in line  # the report never echoes it
    cfg.open.user_agent_contact = ""
    for s in cfg.services:
        s.google_tv = False
        s.launch = None
    assert doctor_mod.contact_check(cfg)[1] is None  # nothing needs Wikidata: not a setup step
