import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

from lumitv import claude_runner, config
from lumitv.claude_runner import ClaudeResult, ClaudeRunner

EXAMPLE = Path(__file__).parent.parent / "lumitv.example.toml"
FAKE = Path(__file__).parent / "fixtures" / "fake_claude.py"
ASK = "abcd1234"


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / ".env").write_text("TMDB_TOKEN=t\nSERVER_TOKEN=s\n")
    c = config.load(EXAMPLE, env_file=tmp_path / ".env")
    c.ask.workdir = tmp_path / "claude-home"
    return c


@pytest.fixture
def out(tmp_path, monkeypatch):
    d = tmp_path / "fake-out"
    d.mkdir()
    monkeypatch.setenv("FAKE_CLAUDE_OUT", str(d))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    # the child env is an allow-list; the fake's own settings must get through it
    monkeypatch.setattr(
        claude_runner, "CHILD_ENV_KEEP_PREFIXES", (*claude_runner.CHILD_ENV_KEEP_PREFIXES, "FAKE_CLAUDE_")
    )
    monkeypatch.setattr(claude_runner, "INTERRUPT_GRACE_S", 0.5)
    monkeypatch.setattr(claude_runner, "STOP_GRACE_S", 0.5)
    monkeypatch.setattr(claude_runner, "RESPAWN_DELAYS_S", (0.05, 0.05, 0.05))
    return d


@pytest.fixture
async def runner(cfg, out):
    r = ClaudeRunner(cfg, "SYSTEM PROMPT", command=[sys.executable, str(FAKE)])
    yield r
    await r.close()


def _read(p: Path) -> dict:
    for _ in range(200):  # on Windows a read can meet the fake's replace of the file: try again
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except PermissionError:
            time.sleep(0.01)
    return json.loads(p.read_text(encoding="utf-8"))


def records(out: Path) -> list[dict]:
    """Every fake process started so far, oldest first."""
    return sorted((_read(p) for p in out.glob("*.json")), key=lambda r: r["started"])


def only(out: Path) -> dict:
    recs = records(out)
    assert len(recs) == 1, recs
    return recs[0]


def alive(pid: int) -> bool:
    if sys.platform == "win32":  # os.kill(pid, 0) is not supported on Windows
        import ctypes

        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        try:
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# Hang guards, not speed checks: they return as soon as the condition holds. Generous, because spawning Python on the
# busy, memory-starved PC can take 10 s.
HANG_S = 60.0


async def wait_for(cond, timeout=HANG_S):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "condition not met in time"
        await asyncio.sleep(0.02)


def sid() -> str:
    return str(uuid.uuid4())


def after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


# --- arguments -------------------------------------------------------------------------------------------------


async def test_new_session_args(runner, out):
    s = sid()
    r = await runner.run("hello", s, False, ASK)
    assert r.ok
    argv = only(out)["argv"]
    assert after(argv, "--session-id") == s and "--resume" not in argv
    assert "--strict-mcp-config" in argv and after(argv, "--model") == "claude-haiku-5-5"
    assert after(argv, "--input-format") == "stream-json" and after(argv, "--output-format") == "stream-json"
    assert "--verbose" in argv and "-p" in argv
    assert after(argv, "--append-system-prompt") == "SYSTEM PROMPT"
    assert after(argv, "--setting-sources") == "local"
    assert after(argv, "--effort") == "low"  # TV asks skip long thinking: speed matters more than depth here


def test_effort_default_leaves_the_cli_default(cfg, tmp_path):
    cfg.ask.effort = "default"
    args = ClaudeRunner(cfg, "S", command=["x"]).build_args(sid(), False, tmp_path / "m.json")
    assert "--effort" not in args
    cfg.ask.effort = "medium"
    assert (
        after(ClaudeRunner(cfg, "S", command=["x"]).build_args(sid(), False, tmp_path / "m.json"), "--effort")
        == "medium"
    )


async def test_followup_resumes(runner, out, tmp_path):
    s = sid()
    args = runner.build_args(s, True, tmp_path / "m.json")
    assert after(args, "--resume") == s and "--session-id" not in args
    r = await runner.run("again", s, True, ASK)  # no process holds s yet: one is started that resumes it
    assert r.ok
    argv = only(out)["argv"]
    assert after(argv, "--resume") == s and "--session-id" not in argv


async def test_only_lumi_tools(runner, out):
    await runner.run("hello", sid(), False, ASK)
    argv = only(out)["argv"]
    assert after(argv, "--allowedTools") == "mcp__lumi"
    assert after(argv, "--tools") == ""  # every built-in tool off


async def test_prompt_goes_via_stdin(runner, out):
    prompt = 'On the TV now: "x"\nThe user typed: something "lighter" ünïcode'
    await runner.run(prompt, sid(), False, ASK)
    rec = only(out)
    assert rec["turns"] == [prompt]
    line = json.loads(rec["stdin"][0])
    assert line["type"] == "user" and line["message"] == {"role": "user", "content": prompt}
    assert rec["stdin"][0].isascii()  # no encoding surprises on Windows pipes
    assert not any(prompt in a or "lighter" in a for a in rec["argv"])


async def test_mcp_file_has_bearer_and_process_header_and_is_deleted(runner, out, cfg):
    await runner.run("hello", sid(), False, ASK)
    rec = only(out)
    lumi = rec["mcp"]["mcpServers"]["lumi"]
    assert lumi["type"] == "http" and lumi["url"] == f"http://127.0.0.1:{cfg.server.port}/mcp"
    tag = lumi["headers"]["X-Lumi-Process"]
    assert lumi["headers"] == {"Authorization": "Bearer s", "X-Lumi-Process": tag}
    assert Path(rec["mcp_path"]).parent == cfg.ask.workdir.resolve()
    if sys.platform != "win32":  # the file holds the token: this user only (Windows inherits the directory's ACL)
        assert Path(rec["mcp_path"]).stat().st_mode & 0o777 == 0o600
    # the process serves the ask: tool calls carrying its header belong to that ask
    assert runner.ask_id_of({"x-lumi-process": tag}) == ASK
    assert runner.ask_id_of({"X-LUMI-PROCESS": tag}) == ASK
    await runner.close()
    assert not list(cfg.ask.workdir.glob("mcp-*.json"))
    # a late call from the gone process still names its (stale) ask, never "no ask" (which the TV would accept)
    assert runner.ask_id_of({"x-lumi-process": tag}) == ASK


async def test_mcp_url_from_config(cfg, out):
    cfg.ask.mcp_url = "http://10.0.0.1:9000/mcp"
    r = ClaudeRunner(cfg, "S", command=[sys.executable, str(FAKE)])
    try:
        await r.run("hello", sid(), False, ASK)
        assert only(out)["mcp"]["mcpServers"]["lumi"]["url"] == "http://10.0.0.1:9000/mcp"
    finally:
        await r.close()


def test_ask_id_of_without_a_known_process(cfg):
    r = ClaudeRunner(cfg, "S", command=["x"])
    assert r.ask_id_of(None) is None
    assert r.ask_id_of({}) is None
    assert r.ask_id_of({"x-lumi-process": "nope"}) is None


# --- results and errors ----------------------------------------------------------------------------------------


async def test_success_parsed(runner):
    r = await runner.run("hello", sid(), False, ASK)
    assert (r.ok, r.text, r.error) == (True, "Here are three", None)
    assert isinstance(r, ClaudeResult) and r.ms >= 0
    assert (r.api_ms, r.turns) == (3, 1)  # Claude Code's own duration_api_ms and num_turns


async def test_login_error_mapped(runner, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "login")
    r = await runner.run("hello", sid(), False, ASK)
    assert (r.ok, r.error) == (False, "login")


async def test_limit_error_mapped(runner, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit")
    r = await runner.run("hello", sid(), False, ASK)
    assert (r.ok, r.error) == (False, "limit")


@pytest.mark.parametrize(
    "mode, error",
    [
        ("login_field", "login"),  # the structured assistant.error flag alone decides
        ("limit_field", "limit"),
        ("billing", "limit"),
        ("login_stderr", "login"),  # with no flag, the message on stderr does
        ("limit_stderr", "limit"),
        ("login_text", "failed"),  # the model's own words never do: a reply mentioning /login is not a logout
        ("limit_text", "failed"),
    ],
)
async def test_failure_classified_from_the_structured_flag_or_stderr_never_from_the_models_words(
    runner, monkeypatch, mode, error
):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    r = await runner.run("hello", sid(), False, ASK)
    assert (r.ok, r.error) == (False, error)


async def test_garbage_output_lines_are_skipped(runner, out):
    r = await runner.run("FAKE:garbage hello", sid(), False, ASK)
    assert (r.ok, r.text) == (True, "Here are three")
    assert only(out)["turns"] == ["FAKE:garbage hello"]


async def test_oversized_output_line_fails_the_turn_and_replaces_the_process(runner, out, monkeypatch):
    """A line over LINE_LIMIT can't be read to its end: the turn fails, the process is killed (its output is
    unreadable from there on) and the next ask resumes the conversation in a new one."""
    monkeypatch.setattr(claude_runner, "LINE_LIMIT", 64 * 1024)
    s = sid()
    assert (await runner.run("one", s, False, ASK)).ok
    r = await asyncio.wait_for(runner.run("FAKE:bigline 200000", s, True, "efgh5678"), HANG_S)
    assert (r.ok, r.error) == (False, "failed")
    pid = only(out)["pid"]
    await wait_for(lambda: not alive(pid))
    assert (await runner.run("again", s, True, "ijkl9012")).ok
    assert after(records(out)[-1]["argv"], "--resume") == s


@pytest.mark.parametrize("mode", ["no_session", "no_session_exit"])
async def test_no_session_mapped(runner, monkeypatch, mode):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    r = await runner.run("hello", sid(), True, ASK)
    assert (r.ok, r.error) == (False, "no_session")


@pytest.mark.parametrize(
    "text, error",
    [
        ("Invalid API key · Please run /login", "login"),
        ("OAuth token has expired", "login"),
        ("You've hit your usage limit", "limit"),
        ("Rate limit exceeded", "limit"),
        ("No conversation found with session ID: x", "no_session"),
        ("API Error: 500", "failed"),
    ],
)
def test_error_text_mapping(text, error):
    assert claude_runner.classify_error(text) == error


async def test_crash_mid_turn_is_failed_and_next_ask_resumes(runner, out):
    s = sid()
    assert (await runner.run("hello", s, False, ASK)).ok
    r = await runner.run("FAKE:crash now", s, True, "efgh5678")
    assert (r.ok, r.error) == (False, "failed")
    r = await runner.run("again", s, True, "ijkl9012")
    assert r.ok
    first, second = records(out)
    assert after(second["argv"], "--resume") == s
    assert not alive(first["pid"])


async def test_missing_binary_is_not_installed(cfg, out):
    r = ClaudeRunner(cfg, "S", command=["/nonexistent/claude"])
    try:
        res = await r.run("hello", sid(), False, ASK)
        assert (res.ok, res.error) == (False, "not_installed")
        await r.start()  # pre-warming without a binary logs, never raises
    finally:
        await r.close()
    assert not list(cfg.ask.workdir.glob("mcp-*.json"))


# --- timeouts and cancelling ----------------------------------------------------------------------------------


async def test_timeout_interrupts_turn_and_keeps_process_warm(runner, out, cfg):
    cfg.ask.timeout_s = 0.5
    s = sid()
    # a "sleep" turn never answers by itself, so "timeout" is the timeout firing
    r = await asyncio.wait_for(runner.run("FAKE:sleep think hard", s, False, ASK), HANG_S)
    assert r.error == "timeout"
    rec = only(out)
    assert any('"interrupt"' in line for line in rec["stdin"])
    assert alive(rec["pid"])
    cfg.ask.timeout_s = HANG_S  # the follow-up is a normal ask
    r = await runner.run("next", s, True, "efgh5678")
    assert (r.ok, r.text) == (True, "Here are three")
    assert len(records(out)) == 1  # same warm process


async def test_timeout_kills_process(runner, out, cfg):
    """A turn that ignores the interrupt: the process is killed and the next ask resumes in a new one."""
    cfg.ask.timeout_s = 0.5
    s = sid()
    r = await asyncio.wait_for(runner.run("FAKE:stubborn think", s, False, ASK), HANG_S)
    assert r.error == "timeout"
    pid = only(out)["pid"]
    await wait_for(lambda: not alive(pid))
    cfg.ask.timeout_s = HANG_S  # the follow-up is a normal ask (in a new process: slow to start on a busy machine)
    assert (await runner.run("next", s, True, "efgh5678")).ok
    assert after(records(out)[-1]["argv"], "--resume") == s


async def test_cancel_kills_running_ask(runner, out):
    task = asyncio.create_task(runner.run("FAKE:sleep think", sid(), False, ASK))
    await wait_for(lambda: records(out) and records(out)[0]["turns"])
    await runner.cancel(ASK)
    r = await asyncio.wait_for(task, HANG_S)  # a "sleep" turn never ends by itself: this is the cancel
    assert (r.ok, r.error) == (False, "failed")


async def test_cancel_of_other_ask_is_ignored(runner, out):
    task = asyncio.create_task(runner.run("hello", sid(), False, ASK))
    await runner.cancel("zzzz9999")
    assert (await task).ok


async def test_cancelled_task_settles_turn_before_next(runner, out):
    """The ask's task is cancelled (a newer ask): the next turn on the process gets its own answer, not the old one."""
    s = sid()
    task = asyncio.create_task(runner.run("FAKE:sleep think", s, False, ASK))
    await wait_for(lambda: records(out) and records(out)[0]["turns"])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    r = await runner.run("newer", s, True, "efgh5678")
    assert (r.ok, r.text) == (True, "Here are three")
    rec = only(out)
    assert rec["turns"] == ["FAKE:sleep think", "newer"]
    assert runner.ask_id_of({"x-lumi-process": rec["mcp"]["mcpServers"]["lumi"]["headers"]["X-Lumi-Process"]}) == (
        "efgh5678"
    )


# --- process lifecycle -----------------------------------------------------------------------------------------


async def test_ask_cancelled_while_waiting_for_an_interrupt_does_not_break_the_next(runner, out):
    """A is cancelled mid-turn (interrupt starts), B is cancelled while waiting for that interrupt: C still works."""
    s = sid()
    a = asyncio.create_task(runner.run("FAKE:sleep A", s, False, "aaaa1111"))
    await wait_for(lambda: records(out) and records(out)[0]["turns"])
    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a
    b = asyncio.create_task(runner.run("B", s, True, "bbbb2222"))
    await asyncio.sleep(0)  # B is now waiting on A's interrupt
    b.cancel()
    with pytest.raises(asyncio.CancelledError):
        await b
    c = await runner.run("C", s, True, "cccc3333")
    assert (c.ok, c.text) == (True, "Here are three")
    rec = only(out)  # the same warm process
    assert rec["turns"] == ["FAKE:sleep A", "C"]


async def test_failed_interrupt_replaces_the_process(runner, out):
    """An interrupt that never finishes (the turn ignores it) ends with the process killed; the next ask resumes."""
    s = sid()
    assert (await runner.run("one", s, False, ASK)).ok
    a = asyncio.create_task(runner.run("FAKE:stubborn A", s, True, "aaaa1111"))
    await wait_for(lambda: len(records(out)[0]["turns"]) == 2)
    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a
    c = await runner.run("C", s, True, "cccc3333")
    assert c.ok
    first, second = records(out)
    assert not alive(first["pid"]) and after(second["argv"], "--resume") == s


async def test_close_during_spawn_leaves_no_process(cfg, out, monkeypatch):
    r = ClaudeRunner(cfg, "S", command=[sys.executable, str(FAKE)])
    real = asyncio.create_subprocess_exec

    async def slow_exec(*a, **kw):
        proc = await real(*a, **kw)
        await r.close()  # shutdown lands while the process is starting
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_exec)
    res = await r.run("hello", sid(), False, ASK)
    assert (res.ok, res.error) == (False, "failed")
    await wait_for(lambda: records(out))
    await wait_for(lambda: not alive(records(out)[0]["pid"]))
    assert not list(cfg.ask.workdir.glob("mcp-*.json"))


def test_stale_mcp_files_are_swept(cfg):
    cfg.ask.workdir.mkdir(parents=True)
    (cfg.ask.workdir / "mcp-dead.json").write_text("{}")
    ClaudeRunner(cfg, "S", command=["x"])
    assert not list(cfg.ask.workdir.glob("mcp-*.json"))


async def test_followup_reuses_the_process(runner, out):
    s = sid()
    await runner.run("one", s, False, ASK)
    await runner.run("two", s, True, "efgh5678")
    assert only(out)["turns"] == ["one", "two"]


async def test_prewarmed_process_answers_first_ask(runner, out, monkeypatch):
    # A cold answer pays the whole start-up (BOOT s) by construction; one from the pre-warmed spare, once it has
    # booted, pays none of it. Waiting for `booted` (not a fixed sleep) keeps this true on a busy machine.
    monkeypatch.setenv("FAKE_CLAUDE_BOOT_S", "3.0")
    await runner.start()
    s = runner.fresh_session_id()
    assert runner.fresh_session_id() == s  # stable until used
    await wait_for(lambda: records(out) and records(out)[0].get("booted"))
    r = await runner.run("hello", s, False, ASK)
    assert r.ok and r.ms < 3000
    first = records(out)[0]
    assert after(first["argv"], "--session-id") == s and first["turns"] == ["hello"]
    # the next conversation gets its own pre-warmed process
    await wait_for(lambda: len(records(out)) == 2)
    assert runner.fresh_session_id() != s
    assert after(records(out)[1]["argv"], "--session-id") == runner.fresh_session_id()


async def test_new_session_stops_previous_process(runner, out):
    await runner.run("one", sid(), False, ASK)
    await runner.run("two", sid(), False, "efgh5678")
    first, second = records(out)
    await wait_for(lambda: not alive(first["pid"]))
    assert alive(second["pid"])


async def test_crashed_spare_is_restarted(runner, out):
    await runner.start()
    await wait_for(lambda: len(records(out)) == 1)
    old = records(out)[0]
    os.kill(old["pid"], 9)
    await wait_for(lambda: len(records(out)) == 2)
    new = records(out)[1]
    assert new["pid"] != old["pid"] and "--session-id" in new["argv"]
    assert runner.fresh_session_id() == after(new["argv"], "--session-id")


async def test_crash_restarted_conversation_is_stopped_when_idle(runner, out, cfg, monkeypatch):
    """A conversation process restarted after a crash is stopped after the idle time too, not kept until the next ask."""
    s = sid()
    assert (await runner.run("hello", s, False, ASK)).ok
    cfg.ask.new_conversation_after_s = 60  # the smallest the config allows ...
    monkeypatch.setattr(claude_runner, "IDLE_SLACK_S", -59.8)  # ... so the idle timer fires 0.2 s after it is armed
    pid = only(out)["pid"]
    os.kill(pid, 9)
    await wait_for(lambda: len(records(out)) == 2)
    restarted = records(out)[1]
    assert after(restarted["argv"], "--resume") == s
    await wait_for(lambda: not alive(restarted["pid"]))


async def test_close_kills_every_process(runner, out, cfg):
    await runner.start()
    await runner.run("one", sid(), False, ASK)
    await wait_for(lambda: len(records(out)) == 2)
    await runner.close()
    for rec in records(out):
        assert not alive(rec["pid"])
    assert not list(cfg.ask.workdir.glob("mcp-*.json"))


async def test_env_quiet_and_cwd_workdir(cfg, out, tmp_path, monkeypatch):
    """The child gets an allow-list of the environment: what it needs to run and Claude Code's own variables; the
    shared PC's keys and this server's secrets stay here."""
    for name, value in (
        ("ANTHROPIC_API_KEY", "sk-would-bill-the-api"), ("ANTHROPIC_AUTH_TOKEN", "x"), ("TMDB_TOKEN", "tmdb-secret"),
        ("SERVER_TOKEN", "s"), ("HF_TOKEN", "hf-secret"), ("AWS_SECRET_ACCESS_KEY", "aws-secret"),
        ("SOME_OTHER_TOOLS_SETTING", "1"), ("HTTPS_PROXY", "http://proxy:3128"), ("no_proxy", "localhost"),
        ("LC_ALL", "C.UTF-8"), ("CLAUDE_CONFIG_DIR", str(tmp_path / "cc")),
    ):  # fmt: skip
        monkeypatch.setenv(name, value)
    (tmp_path / ".env").write_text("TMDB_TOKEN=t\nSERVER_TOKEN=s\nCLAUDE_CODE_OAUTH_TOKEN=oat-secret\n")
    c = config.load(EXAMPLE, env_file=tmp_path / ".env")
    c.ask.workdir = cfg.ask.workdir
    r = ClaudeRunner(c, "S", command=[sys.executable, str(FAKE)])
    try:
        await r.run("hello", sid(), False, ASK)
    finally:
        await r.close()
    rec = only(out)
    keys = {k.upper() for k in rec["env_keys"]}  # Windows upper-cases the names
    assert not keys & {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "TMDB_TOKEN", "SERVER_TOKEN", "HF_TOKEN",
                       "AWS_SECRET_ACCESS_KEY", "SOME_OTHER_TOOLS_SETTING"}  # fmt: skip
    assert {"PATH", "HTTPS_PROXY", "NO_PROXY", "LC_ALL", "CLAUDE_CONFIG_DIR"} <= keys
    assert keys & {"HOME", "USERPROFILE"}  # Node's home directory
    assert rec["env"] == {
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_OAUTH_TOKEN": "oat-secret",
    }
    assert Path(rec["cwd"]).resolve() == cfg.ask.workdir.resolve()
    assert not (cfg.ask.workdir / "CLAUDE.md").exists()
    assert "oat-secret" not in repr(c)


def test_child_env_allow_list_is_case_insensitive(monkeypatch):
    """Windows variable names come in any case; the Python process on the PC sees them upper-cased, a shell may not."""
    monkeypatch.setenv("http_proxy", "p")
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    monkeypatch.setenv("Hf_Token", "secret")
    env = {k.upper(): v for k, v in claude_runner.child_env().items()}  # Windows upper-cases the names
    assert env["HTTP_PROXY"] == "p" and env["SYSTEMROOT"] == "C:\\Windows" and "HF_TOKEN" not in env


async def test_token_never_logged(runner, out, caplog, monkeypatch):
    caplog.set_level("DEBUG", logger="lumitv.claude")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "login")
    await runner.start()
    await runner.run("hello", sid(), False, ASK)
    await runner.close()
    assert "Bearer" not in caplog.text and "Bearer s" not in caplog.text
