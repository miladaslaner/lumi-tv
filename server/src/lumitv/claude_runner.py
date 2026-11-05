"""Claude Code (`claude -p`) answering asks from the TV, with only this server's MCP tools.

A fresh `claude -p` per ask costs 10–20 s of start-up on the server PC while the model answers in 2–3 s, so Claude Code
runs as long-lived processes in stream-json mode (`--input-format stream-json --output-format stream-json`): each ask
is one user line written to the process' stdin, answered by a `result` line on its stdout.

- **spare**: once `start()` was called, one process waits for the next conversation, started ahead of use with
  `--session-id <fresh_session_id()>`. A new conversation takes it over and a new spare is started at once.
- **active**: the process holding the current conversation; follow-up asks are further turns on it. It is stopped
  when a new conversation takes over, or after `new_conversation_after_s` without an ask.
- A process that dies on its own is replaced after a short delay (the spare by a new spare; the active one, if it
  had answered before, by a process resuming its session with `--resume`). `close()` stops everything.
- A running turn is stopped (timeout, `cancel`, the ask's task cancelled) with the CLI's `interrupt` control request
  (`{"type": "control_request", "request": {"subtype": "interrupt"}}`, the Agent SDK's stream-json wire protocol,
  checked on Claude Code 2.1.318), which keeps the process and its conversation warm; a process that doesn't stop
  within `INTERRUPT_GRACE_S` is killed and, if it held a conversation that had answered, replaced at once by one
  resuming it. Asks waiting for an interrupt wait shielded, so cancelling them never cancels the interrupt itself.
- A failed turn is explained to the TV from the CLI's structured fields first (`assistant.error`, then the
  `result`'s `errors` list and stderr by substring); the model's own words are never read for it.
- The child env is built from an allow-list (`CHILD_ENV_KEEP*`): what Node and the CLI need, plus Claude Code's own
  variables. `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` (they would bill the API, not the subscription), this
  server's secrets and the rest of the shared PC's environment stay here. `--setting-sources local` also keeps the
  user's and parent directories' CLAUDE.md out (checked on Claude Code 2.1.318).

Which ask a tool call belongs to: the MCP config (URL, headers) is fixed for a process' whole life, so it can't carry
a per-ask header. Each process gets a random tag sent as the `X-Lumi-Process` header on every MCP request, and
`ask_id_of(headers)` maps it to the ask the process is answering (or answered last). A tool call that sneaks out of an
interrupted turn therefore carries the old ask id, which the TV ignores. Claude Desktop sends no such header → None.
"""

import asyncio
import collections
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .config import Config

log = logging.getLogger("lumitv.claude")

ClaudeError = Literal["failed", "timeout", "login", "limit", "not_installed", "no_session"]

PROCESS_HEADER = "X-Lumi-Process"
INTERRUPT_GRACE_S = 3.0  # an interrupted turn must end this soon, or the process is killed
STOP_GRACE_S = 2.0  # a process asked to quit (stdin closed) must exit this soon, or it is killed
RESPAWN_DELAYS_S = (2.0, 10.0, 60.0)  # after 1, 2, 3 unexpected exits in a row; then no more until an ask
STDERR_TAIL = 4096
RETIRED_TAGS = 16  # gone processes whose last ask is still known
IDLE_SLACK_S = (
    5.0  # the conversation's process lives this long past new_conversation_after_s: a late ask still finds it
)
LINE_LIMIT = 16 * 1024 * 1024  # one stream-json line (a long tool result) may be large
# What the child gets of this process' environment (names compared upper-cased: Windows ones are case-insensitive):
# the system and profile directories Node and the CLI need, locale, proxies and TLS trust, Claude Code's own
# variables. Everything else on the shared PC (cloud keys, HF_TOKEN, this server's secrets, an ANTHROPIC_API_KEY that
# would bill the API instead of the subscription) stays here.
CHILD_ENV_KEEP = frozenset({
    "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "OS", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "ALLUSERSPROFILE", "PUBLIC", "TEMP", "TMP", "TMPDIR", "USERNAME",
    "USER", "LOGNAME", "USERDOMAIN", "COMPUTERNAME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "SHELL",
    "LANG", "LANGUAGE", "TERM", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
})  # fmt: skip
CHILD_ENV_KEEP_PREFIXES = ("LC_", "XDG_", "CLAUDE_", "PROGRAMFILES", "COMMONPROGRAMFILES")
CHILD_ENV_KEEP_SUFFIXES = ("_PROXY",)

_LOGIN = ("/login", "not logged in", "invalid api key", "oauth")
_LIMIT = ("usage limit", "limit reached", "rate limit")
_NO_SESSION = ("no conversation found",)
# `assistant.error` as Claude Code sets it on an API failure (the structured signal; checked on 2.1.318)
_ASSISTANT_ERRORS: dict[str, ClaudeError] = {
    "authentication_failed": "login",
    "rate_limit": "limit",
    "billing_error": "limit",
}
_CANCEL = object()  # queued by cancel(): stop the running turn
_LOOP_HINT = " (this event loop can't run subprocesses; on Windows the server needs the Proactor loop)"


def child_env() -> dict[str, str]:
    """This process' environment reduced to CHILD_ENV_KEEP*, for Claude Code."""

    def kept(name: str) -> bool:
        n = name.upper()
        return n in CHILD_ENV_KEEP or n.startswith(CHILD_ENV_KEEP_PREFIXES) or n.endswith(CHILD_ENV_KEEP_SUFFIXES)

    return {k: v for k, v in os.environ.items() if kept(k)}


def classify_error(text: str) -> ClaudeError:
    """Map what a failed run said in its error channels (the `result`'s `errors` list, stderr) to an error the TV can
    explain. The last resort after `assistant.error`; never fed the model's own words, which may say "rate limit" or
    "oauth" without either having happened."""
    t = text.lower()
    if any(k in t for k in _LOGIN):
        return "login"
    if any(k in t for k in _LIMIT):
        return "limit"
    if any(k in t for k in _NO_SESSION):
        return "no_session"
    return "failed"


@dataclass
class ClaudeResult:
    ok: bool
    text: str
    error: ClaudeError | None
    ms: float
    api_ms: float | None = None  # Claude Code's own `duration_api_ms`: time in Anthropic's API (vs. our overhead)
    turns: int | None = None  # Claude Code's `num_turns`: model round trips (each tool call adds one)


@dataclass(eq=False)
class _Proc:
    tag: str
    session_id: str
    proc: asyncio.subprocess.Process
    mcp_file: Path
    messages: asyncio.Queue = field(default_factory=asyncio.Queue)  # parsed stdout lines; None at end of output
    stderr: bytearray = field(default_factory=bytearray)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # one turn at a time
    ask_id: str | None = None
    in_turn: bool = False
    ok_turns: int = 0
    eof: bool = False
    stopping: bool = False  # retired on purpose: its exit is not a crash
    settling: asyncio.Task | None = None  # an abandoned turn being interrupted
    tasks: list[asyncio.Task] = field(default_factory=list)

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None and not self.eof and not self.stopping


class ClaudeRunner:
    def __init__(self, cfg: Config, system_prompt: str, command: list[str] | None = None):
        self._cfg = cfg
        self._system_prompt = system_prompt
        self._command = command
        self._workdir = cfg.ask.workdir.resolve()
        self._mcp_url = cfg.ask.mcp_url or f"http://127.0.0.1:{cfg.server.port}/mcp"
        self._spare: _Proc | None = None
        self._spare_id: str | None = None
        self._active: _Proc | None = None
        self._by_tag: dict[str, _Proc] = {}
        self._retired: collections.OrderedDict[str, str | None] = collections.OrderedDict()  # tag -> last ask id
        self._spawn_lock = asyncio.Lock()
        self._prewarm = False
        self._closed = False
        self._crashes = 0
        self._cancelled: collections.deque[str] = collections.deque(maxlen=32)  # asks cancelled before their turn
        self._idle: asyncio.TimerHandle | None = None
        self._bg: set[asyncio.Task] = set()
        self._requests = 0
        for stale in self._workdir.glob("mcp-*.json"):  # left by a server that was killed hard
            stale.unlink(missing_ok=True)

    # --- public ---------------------------------------------------------------------------------------------

    def build_args(self, session_id: str, resume: bool, mcp_config: Path) -> list[str]:
        """Claude Code arguments for one long-lived TV process. The question never goes on the command line."""
        return [
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",  # the CLI requires it for stream-json output with -p
            "--model", self._cfg.ask.model,
            *(["--effort", self._cfg.ask.effort] if self._cfg.ask.effort != "default" else []),
            "--mcp-config", str(mcp_config),
            "--strict-mcp-config",
            "--allowedTools", "mcp__lumi",
            "--tools", "",
            "--setting-sources", "local",  # not the PC user's settings, hooks or plugins; the workdir has none
            "--append-system-prompt", self._system_prompt,
            *(["--resume", session_id] if resume else ["--session-id", session_id]),
        ]  # fmt: skip

    async def start(self) -> None:
        """Pre-warm: keep a process ready for the next conversation from now on. Never raises."""
        if self._closed:
            return
        self._prewarm = True
        await self._ensure_spare()

    def fresh_session_id(self) -> str:
        """The session id the next new conversation should use: the pre-warmed process' (reserved until used)."""
        if self._spare_id is None:
            self._spare_id = str(uuid.uuid4())
        return self._spare_id

    def ask_id_of(self, headers: Mapping[str, str] | None) -> str | None:
        """The ask a tool call belongs to, from its MCP request headers; None when it isn't from a TV ask."""
        if not headers:
            return None
        tag = next((v for k, v in headers.items() if k.lower() == PROCESS_HEADER.lower()), None)
        if not tag:
            return None
        p = self._by_tag.get(tag)
        # a late call from a process already gone still names its (stale) ask, so the TV ignores it
        return p.ask_id if p else self._retired.get(tag)

    async def run(self, prompt: str, session_id: str, resume: bool, ask_id: str) -> ClaudeResult:
        """One ask as a turn of the conversation `session_id`. Never raises (except when the task is cancelled)."""
        t0 = time.monotonic()
        if self._idle:
            self._idle.cancel()
        try:
            for _ in range(2):  # a second try when the chosen process died while we waited for it
                try:
                    p = await self._process_for(session_id, resume)
                except (OSError, NotImplementedError, ValueError) as e:
                    log.warning("claude could not start (%s): %s%s", self._executable(), type(e).__name__,
                                _LOOP_HINT if isinstance(e, NotImplementedError) else "")  # fmt: skip
                    # no CLI at [ask] claude_path: the TV says Lumi isn't set up on the server
                    return self._result(
                        t0, False, "", "not_installed" if isinstance(e, FileNotFoundError) else "failed"
                    )
                async with p.lock:
                    await self._await_settled(p)
                    if ask_id in self._cancelled:
                        self._cancelled.remove(ask_id)
                        return self._result(t0, False, "", "failed")
                    if not p.alive:
                        continue
                    return await self._turn(p, prompt, ask_id, t0)
            return self._result(t0, False, "", "failed")
        finally:
            self._arm_idle_timer()

    async def cancel(self, ask_id: str) -> None:
        """Stop that ask's turn if it is running (the process stays warm); an ask not started yet won't start."""
        for p in self._by_tag.values():
            if p.in_turn and p.ask_id == ask_id:
                p.messages.put_nowait(_CANCEL)
                return
        self._cancelled.append(ask_id)

    async def close(self) -> None:
        """Stop every Claude process (server shutdown)."""
        self._closed = True
        if self._idle:
            self._idle.cancel()
        for t in list(self._bg):
            t.cancel()
        procs = {p for p in (self._spare, self._active) if p} | set(self._by_tag.values())
        self._spare = self._active = None
        await asyncio.gather(*(self._stop(p) for p in procs), return_exceptions=True)
        for p in procs:
            for t in p.tasks:
                if not t.done():
                    t.cancel()
            p.mcp_file.unlink(missing_ok=True)

    # --- turns ----------------------------------------------------------------------------------------------

    async def _turn(self, p: _Proc, prompt: str, ask_id: str, t0: float) -> ClaudeResult:
        while not p.messages.empty():  # leftovers between turns are not this ask's
            p.messages.get_nowait()
        p.ask_id, p.in_turn = ask_id, True
        line = {"type": "user", "message": {"role": "user", "content": prompt}, "parent_tool_use_id": None,
                "session_id": ""}  # fmt: skip
        flags: list[str] = []  # `assistant.error` values seen this turn
        try:
            try:
                await self._send(p, line)
            except (ConnectionError, OSError):
                pass  # the process is gone; its end of output tells us why
            while True:
                remaining = t0 + self._cfg.ask.timeout_s - time.monotonic()
                try:
                    msg = await asyncio.wait_for(p.messages.get(), max(remaining, 0))
                except TimeoutError:
                    log.warning("claude turn timed out (ask %s)", ask_id)
                    await self._settle(p)
                    return self._result(t0, False, "", "timeout")
                if msg is _CANCEL:
                    await self._settle(p)
                    return self._result(t0, False, "", "failed")
                if msg is None:  # the process ended mid-turn
                    await self._reap(p)
                    error = _flagged(flags) or classify_error(p.stderr.decode("utf-8", "replace"))
                    log.warning("claude exited during a turn (ask %s, code %s): %s", ask_id, p.proc.returncode, error)
                    return self._result(t0, False, "", error)
                kind = msg.get("type")
                if kind == "assistant":
                    if (flag := msg.get("error")) in _ASSISTANT_ERRORS:
                        flags.append(flag)
                elif kind == "result":
                    result = msg.get("result")
                    text = result if isinstance(result, str) else ""
                    if not msg.get("is_error"):
                        p.ok_turns += 1
                        self._crashes = 0
                        api_ms, turns = msg.get("duration_api_ms"), msg.get("num_turns")
                        return self._result(
                            t0, True, text.strip(), None,
                            api_ms if isinstance(api_ms, (int, float)) else None,
                            turns if isinstance(turns, int) else None,
                        )  # fmt: skip
                    errors = [e for e in msg.get("errors") or [] if isinstance(e, str)]
                    error = _flagged(flags) or classify_error("\n".join([*errors, p.stderr.decode("utf-8", "replace")]))
                    log.warning("claude turn failed (ask %s, %s): %s", ask_id, msg.get("subtype"), error)
                    return self._result(t0, False, "", error)
        except asyncio.CancelledError:
            # the ask was superseded: interrupt the turn in the background; the next turn on p waits for it
            p.settling = asyncio.ensure_future(self._settle(p))
            raise
        finally:
            p.in_turn = False

    async def _await_settled(self, p: _Proc) -> None:
        """Wait for an abandoned turn's interrupt to finish. Shielded: if this ask is cancelled while waiting, the
        interrupt carries on for the next ask. An interrupt that was itself cancelled or failed retires the process,
        since its old turn may still be running."""
        settling = p.settling
        if settling is None:
            return
        try:
            await asyncio.shield(settling)
            failed = False
        except asyncio.CancelledError:
            if not settling.done():
                raise  # this ask was cancelled; the interrupt goes on
            failed = True
        except Exception:
            failed = True
        if p.settling is settling:
            p.settling = None
        if failed:
            log.warning("claude process %s: interrupting its old turn failed; replacing it", p.tag)
            await self._kill_and_replace(p)

    async def _settle(self, p: _Proc) -> None:
        """Interrupt the running turn and wait for its end; kill the process if it won't stop."""
        if not p.alive:
            return
        self._requests += 1
        try:
            await self._send(p, {"type": "control_request", "request_id": f"lumi-{self._requests}",
                                 "request": {"subtype": "interrupt"}})  # fmt: skip
            async with asyncio.timeout(INTERRUPT_GRACE_S):
                while (msg := await p.messages.get()) is not None:
                    if isinstance(msg, dict) and msg.get("type") == "result":
                        return
        except (TimeoutError, ConnectionError, OSError):
            pass
        log.warning("claude process %s did not stop its turn; killing it", p.tag)
        await self._kill_and_replace(p)

    async def _kill_and_replace(self, p: _Proc) -> None:
        """Kill a process on purpose (not a crash). If it held the conversation and had answered, a process resuming
        that conversation is started at once so the next ask finds it warm."""
        p.stopping = True
        await self._kill(p)
        if self._active is p:
            self._active = None
            if p.ok_turns and not self._closed:
                self._background(self._respawn("active", p.session_id, 0))

    async def _send(self, p: _Proc, obj: dict) -> None:
        assert p.proc.stdin is not None
        p.proc.stdin.write((json.dumps(obj) + "\n").encode("ascii"))  # ASCII-only JSON: no encoding issues
        await p.proc.stdin.drain()

    @staticmethod
    def _result(
        t0: float, ok: bool, text: str, error: ClaudeError | None, api_ms: float | None = None, turns: int | None = None
    ) -> ClaudeResult:
        return ClaudeResult(ok, text, error, (time.monotonic() - t0) * 1000, api_ms, turns)

    # --- processes ------------------------------------------------------------------------------------------

    async def _process_for(self, session_id: str, resume: bool) -> _Proc:
        async with self._spawn_lock:
            if self._closed:
                raise ValueError("closed")
            old = self._active
            if old and old.session_id == session_id and old.alive:
                return old
            spare = self._spare
            if spare and spare.session_id == session_id and spare.alive:
                self._spare, self._spare_id = None, None
                p = spare
            else:
                p = await self._spawn(session_id, resume, "active")
                if session_id == self._spare_id:
                    self._spare_id = None
            self._active = p
            if old and old is not p:
                self._background(self._stop(old))
            if self._prewarm and self._spare is None:
                self._background(self._ensure_spare())
            return p

    async def _ensure_spare(self) -> None:
        async with self._spawn_lock:
            if self._closed or (self._spare and self._spare.alive):
                return
            try:
                self._spare = await self._spawn(self.fresh_session_id(), False, "spare")
            except (OSError, NotImplementedError, ValueError) as e:
                self._spare = None
                log.warning("claude could not be pre-warmed (%s): %s", self._executable(), type(e).__name__)

    async def _respawn(self, role: str, session_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        if role == "spare":
            if self._prewarm:
                await self._ensure_spare()
            return
        async with self._spawn_lock:
            if self._closed or self._active is not None:
                return
            try:
                self._active = await self._spawn(session_id, True, "active")
            except (OSError, NotImplementedError, ValueError) as e:
                log.warning("claude could not be restarted: %s", type(e).__name__)
                return
        self._arm_idle_timer()  # a restarted conversation is stopped when idle, like one an ask left

    def _executable(self) -> str:
        return self._command[0] if self._command else self._cfg.ask.claude_path

    async def _spawn(self, session_id: str, resume: bool, role: str) -> _Proc:
        tag = secrets.token_hex(8)
        self._workdir.mkdir(parents=True, exist_ok=True)
        mcp_file = self._workdir / f"mcp-{tag}.json"
        _write_private(mcp_file, json.dumps({"mcpServers": {"lumi": {
            "type": "http", "url": self._mcp_url,
            "headers": {"Authorization": f"Bearer {self._cfg.server_token}", PROCESS_HEADER: tag},
        }}}))  # fmt: skip
        if self._command:
            command = list(self._command)
        else:  # resolve like a shell would (PATHEXT on Windows); a missing CLI raises FileNotFoundError below
            command = [shutil.which(self._cfg.ask.claude_path) or self._cfg.ask.claude_path]
        env = child_env() | {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}
        if self._cfg.claude_oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self._cfg.claude_oauth_token
        if sys.platform == "win32":
            group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
        else:
            group = {"start_new_session": True}
        try:
            proc = await asyncio.create_subprocess_exec(
                *command, *self.build_args(session_id, resume, mcp_file),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=self._workdir, env=env, limit=LINE_LIMIT, **group,
            )  # fmt: skip
        except BaseException:
            mcp_file.unlink(missing_ok=True)
            raise
        p = _Proc(tag, session_id, proc, mcp_file)
        self._by_tag[tag] = p
        p.tasks = [asyncio.ensure_future(c) for c in (self._read_stdout(p), self._read_stderr(p), self._watch(p))]
        if self._closed:  # close() ran while this process was starting
            await self._stop(p)
            mcp_file.unlink(missing_ok=True)
            raise ValueError("closed")
        log.info("claude process started: %s %s pid=%s session=%s%s", role, tag, proc.pid, session_id,
                 " (resume)" if resume else "")  # fmt: skip
        return p

    async def _read_stdout(self, p: _Proc) -> None:
        assert p.proc.stdout is not None
        try:
            while line := await p.proc.stdout.readline():
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue  # not a protocol line
                if isinstance(msg, dict):
                    p.messages.put_nowait(msg)
        except (ValueError, ConnectionError, OSError) as e:  # ValueError: a line over LINE_LIMIT
            log.warning("claude process %s output unreadable: %s", p.tag, type(e).__name__)
        finally:
            p.eof = True
            p.messages.put_nowait(None)

    async def _read_stderr(self, p: _Proc) -> None:
        assert p.proc.stderr is not None
        while chunk := await p.proc.stderr.read(4096):
            p.stderr += chunk
            del p.stderr[:-STDERR_TAIL]

    async def _watch(self, p: _Proc) -> None:
        code = await p.proc.wait()
        p.mcp_file.unlink(missing_ok=True)
        self._retire_tag(p)
        if p.stopping or self._closed:
            log.info("claude process %s exited (code %s)", p.tag, code)
            return
        log.warning("claude process %s exited unexpectedly (code %s)", p.tag, code)
        role = None
        if self._spare is p:
            self._spare, self._spare_id, role = None, None, "spare"
        elif self._active is p:
            self._active = None
            role = "active" if p.ok_turns else None  # never answered: nothing worth resuming
        if role is None:
            return
        self._crashes += 1
        if self._crashes > len(RESPAWN_DELAYS_S):
            log.error("claude keeps exiting; not restarting it until the next ask")
            return
        self._background(self._respawn(role, p.session_id, RESPAWN_DELAYS_S[self._crashes - 1]))

    async def _stop(self, p: _Proc) -> None:
        """Retire a process: close its input so it quits cleanly, kill it if it doesn't."""
        p.stopping = True
        if p.proc.returncode is None and p.proc.stdin is not None:
            try:
                p.proc.stdin.close()
                await asyncio.wait_for(p.proc.wait(), STOP_GRACE_S)
            except (TimeoutError, ConnectionError, OSError):
                pass
        await self._kill(p)

    async def _reap(self, p: _Proc) -> None:
        """The process ended its output: wait for its exit (kill it if it lingers)."""
        try:
            await asyncio.wait_for(p.proc.wait(), STOP_GRACE_S)
        except TimeoutError:
            await self._kill(p)

    async def _kill(self, p: _Proc) -> None:
        """Kill the process and its children."""
        if p.proc.returncode is None:
            try:
                if sys.platform == "win32":
                    await asyncio.to_thread(subprocess.run, ["taskkill", "/PID", str(p.proc.pid), "/T", "/F"],
                                            capture_output=True, timeout=10, check=False,
                                            creationflags=subprocess.CREATE_NO_WINDOW)  # fmt: skip
                else:
                    os.killpg(p.proc.pid, signal.SIGKILL)
            except (OSError, subprocess.SubprocessError):
                pass
            if p.proc.returncode is None:
                try:
                    p.proc.kill()
                except ProcessLookupError:
                    pass
        await p.proc.wait()

    def _retire_tag(self, p: _Proc) -> None:
        if self._by_tag.pop(p.tag, None) is not None and p.ask_id:
            self._retired[p.tag] = p.ask_id
            while len(self._retired) > RETIRED_TAGS:
                self._retired.popitem(last=False)

    def _arm_idle_timer(self) -> None:
        """Stop the conversation's process once no ask can continue it any more."""
        if self._idle:
            self._idle.cancel()
        if self._active and not self._closed:
            loop = asyncio.get_running_loop()
            self._idle = loop.call_later(self._cfg.ask.new_conversation_after_s + IDLE_SLACK_S, self._on_idle)

    def _on_idle(self) -> None:
        p = self._active
        if p and not p.lock.locked():
            self._active = None
            self._background(self._stop(p))

    def _background(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)


def _flagged(flags: list[str]) -> ClaudeError | None:
    """The error the first `assistant.error` flag of a turn names, if any."""
    return _ASSISTANT_ERRORS[flags[0]] if flags else None


def _write_private(path: Path, text: str) -> None:
    """Write UTF-8 (no BOM): the file holds the server token. POSIX: mode 0o600, this user only. Windows: the mode
    only sets the read-only attribute, so the file inherits the directory's ACL (user-only under the profile
    directory; a workdir elsewhere is as private as that directory)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
