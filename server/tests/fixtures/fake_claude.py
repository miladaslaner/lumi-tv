"""Stand-in for the Claude Code CLI in stream-json mode, for ClaudeRunner tests.

Speaks the same protocol as `claude -p --input-format stream-json --output-format stream-json --verbose`: one JSON
object per line. Each `{"type": "user", ...}` line is a turn answered with `system/init`, `assistant` and `result`
lines; `{"type": "control_request", "request": {"subtype": "interrupt"}}` stops a running turn (`control_response`,
then a `result` with subtype `error_during_execution`). The process lives until stdin closes.

Environment:
- FAKE_CLAUDE_OUT: a directory; this process writes `<pid>.json` there (argv, cwd, selected env, the MCP config file's
  contents, and every turn's stdin line), rewritten after each change.
- FAKE_CLAUDE_MODE: how turns are answered: `ok` (default), `login`, `limit` (as Claude Code 2.1: the structured
  `assistant.error` flag and the message as text), `login_field` / `limit_field` / `billing` (the flag alone, generic
  text), `login_text` / `limit_text` (the message as the model's words alone, no flag, nothing on stderr),
  `login_stderr` / `limit_stderr` (the message on stderr alone), `sleep` (no answer until interrupted), `stubborn`
  (ignores interrupts), `crash` (exits mid-turn), `garbage` (lines that are not protocol before a normal answer),
  `bigline` (one stdout line of `FAKE:bigline <bytes>` bytes, longer than the reader's limit), `no_session` (with
  --resume: answers the first turn with an error result naming the unknown session, then exits, like Claude Code
  2.1), `no_session_exit` (with --resume: exits at start with the message on stderr only).
- FAKE_CLAUDE_BOOT_S: seconds of start-up before stdin is read (a cold Claude Code start); the record then has
  `booted`.
A turn whose text starts with `FAKE:<mode> ` uses that mode for this turn only.
"""

import json
import os
import sys
import time
from pathlib import Path

ENV_KEYS = ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_AUTOUPDATER", "CLAUDE_CODE_OAUTH_TOKEN")
REPLY = "Here are three"
LOGIN = "Not logged in · Please run /login"
LIMIT = "Claude AI usage limit reached|1762300800"
FLAGS = {"login": "authentication_failed", "limit": "rate_limit", "billing": "billing_error"}
MESSAGES = {"login": LOGIN, "limit": LIMIT, "billing": "Billing error"}


def arg_after(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


def main() -> None:
    argv = sys.argv[1:]
    out_dir = os.environ["FAKE_CLAUDE_OUT"]
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
    mcp_path = arg_after(argv, "--mcp-config")
    record = {
        "pid": os.getpid(),
        "started": time.time(),
        "argv": argv,
        "cwd": os.getcwd(),
        "env": {k: os.environ.get(k) for k in ENV_KEYS},
        "env_keys": sorted(os.environ),
        "mcp": json.loads(Path(mcp_path).read_text(encoding="utf-8")) if mcp_path else None,
        "mcp_path": mcp_path,
        "stdin": [],
        "turns": [],
    }

    def save() -> None:
        path = os.path.join(out_dir, f"{os.getpid()}.json")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(record, f)
        for _ in range(200):  # Windows refuses the replace while the test is reading the file: wait it out
            try:
                os.replace(path + ".tmp", path)
                return
            except PermissionError:
                time.sleep(0.01)
        os.replace(path + ".tmp", path)

    session = arg_after(argv, "--session-id") or arg_after(argv, "--resume") or "fake-session"

    def emit(obj: dict) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def result(text: str | None, is_error: bool, subtype: str = "success") -> None:
        emit({"type": "result", "subtype": subtype, "is_error": is_error, "result": text, "session_id": session,
              "duration_ms": 5, "duration_api_ms": 3, "num_turns": 1})  # fmt: skip

    def assistant(text: str, error: str | None = None) -> None:
        msg = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
               "session_id": session}  # fmt: skip
        if error:
            msg["error"] = error
        emit(msg)

    save()
    time.sleep(float(os.environ.get("FAKE_CLAUDE_BOOT_S", "0")))
    record["booted"] = True
    save()
    if mode == "no_session_exit" and "--resume" in argv:  # older CLIs: a message on stderr, before any input
        sys.stderr.write(f"No conversation found with session ID: {session}\n")
        sys.stderr.flush()
        sys.exit(1)

    running = None  # mode of the turn waiting for an interrupt
    for line in sys.stdin:
        record["stdin"].append(line)
        msg = json.loads(line)
        if msg["type"] == "control_request":
            save()  # before answering, so a test that saw the answer sees the record
            emit({"type": "control_response",
                  "response": {"subtype": "success", "request_id": msg["request_id"], "response": {}}})  # fmt: skip
            if msg["request"]["subtype"] == "interrupt" and running == "sleep":
                running = None
                result(None, True, "error_during_execution")
            continue
        text = msg["message"]["content"]
        record["turns"].append(text)
        save()
        turn_mode = mode
        if text.startswith("FAKE:"):
            turn_mode = text.split()[0].removeprefix("FAKE:")
        if mode == "no_session" and "--resume" in argv:  # what Claude Code 2.1 does for an unknown session id
            message = f"No conversation found with session ID: {session}"
            emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0,
                  "session_id": session, "errors": [message]})  # fmt: skip
            sys.stderr.write(message + "\n")
            sys.exit(1)
        emit({"type": "system", "subtype": "init", "session_id": session, "model": arg_after(argv, "--model")})
        failure, _, channel = turn_mode.partition("_")
        if failure in FLAGS:
            if channel == "stderr":  # the message on stderr alone, then a bare error result
                sys.stderr.write(MESSAGES[failure] + "\n")
                sys.stderr.flush()
                time.sleep(0.05)  # the CLI prints the error before it ends the turn
                assistant("")
                result("Request failed", True)
            elif channel == "text":  # the message as the model's words alone
                assistant(MESSAGES[failure])
                result(MESSAGES[failure], True)
            elif channel == "field" or failure == "billing":  # the structured flag alone
                assistant("", error=FLAGS[failure])
                result("Request failed", True)
            else:  # as Claude Code 2.1: the flag and the message
                assistant(MESSAGES[failure], error=FLAGS[failure])
                result(MESSAGES[failure], True)
        elif turn_mode in ("sleep", "stubborn"):
            running = turn_mode
        elif turn_mode == "crash":
            sys.stderr.write("boom\n")
            sys.exit(3)
        elif turn_mode == "garbage":  # not protocol lines: the reader skips them
            sys.stdout.write('not json\n[1, 2]\n"a string"\n\n')
            sys.stdout.flush()
            assistant(REPLY)
            result(REPLY, False)
        elif turn_mode == "bigline":  # one line over the reader's limit (never read to its end)
            sys.stdout.write("x" * int(text.split()[1]) + "\n")
            sys.stdout.flush()
            result(REPLY, False)
        else:
            assistant(REPLY)
            result(REPLY, False)


if __name__ == "__main__":
    main()
