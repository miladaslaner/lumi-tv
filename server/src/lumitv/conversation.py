"""TV conversations: which Claude session an ask continues, and the message Claude gets for it."""

import time
import uuid
from collections.abc import Callable
from typing import Literal

from .claude_runner import ClaudeError
from .models import Viewer
from .tvlink import ScreenContext


class Conversations:
    """One conversation at a time. An ask continues it while the last successful ask was at most `quiet_s` ago;
    otherwise (or when there is none) a new session starts. `new_session_id` names new sessions; the server passes
    `ClaudeRunner.fresh_session_id` so a new conversation lands on the pre-warmed Claude process."""

    def __init__(
        self,
        quiet_s: float,
        clock: Callable[[], float] = time.monotonic,
        new_session_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ):
        self._quiet_s = quiet_s
        self._clock = clock
        self._new_id = new_session_id
        self._session: str | None = None
        self._confirmed = False  # Claude answered at least once in this session
        self._last_at: float | None = None

    def begin(self) -> tuple[str, bool]:
        """(session id, resume) for the next ask."""
        stale = self._last_at is not None and self._clock() - self._last_at > self._quiet_s
        if self._session is None or stale:
            self._session, self._confirmed, self._last_at = self._new_id(), False, None
        return self._session, self._confirmed

    def finish(self, session_id: str, ok: bool, error: ClaudeError | None) -> None:
        if session_id != self._session:
            return
        if ok:
            self._confirmed, self._last_at = True, self._clock()
        elif error == "no_session" or not self._confirmed:
            self._session, self._confirmed, self._last_at = None, False, None


def build_prompt(
    question: str, source: Literal["voice", "typed"], screen: ScreenContext | None, watching: list[Viewer] | None = None
) -> str:
    """The message for one ask: who is watching and what is on the TV now (when known), then the question."""
    lines = []
    if watching:
        lines.append("Watching: " + ", ".join(v.label() for v in watching) + ".")
    if screen is not None:
        line = f'On the TV now: "{screen.headline}"'
        if screen.titles:
            line += ": " + ", ".join(f"{title} ({kind}:{tmdb_id})" for title, kind, tmdb_id in screen.titles)
        line += "."
        if screen.focused:
            line += f" Focused: {screen.focused}."
        lines.append(line)
    if source == "voice":
        lines.append(f"The user said (speech-to-text, may contain errors): {question}")
    else:
        lines.append(f"The user typed: {question}")
    return "\n".join(lines)
