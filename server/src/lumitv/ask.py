"""Questions asked on the TV itself (`POST /ask`), answered by Claude Code through this server's own MCP tools.

One ask: noise is turned away (`not_heard`, Claude never runs) → `heard` → Claude Code answers in the TV
conversation, seeded with what is on the screen → its `show_on_tv` call puts the posters up, or its words go to the TV
as `ask_reply`, or the TV is told why it failed (`ask_error`). The newest ask wins: a new one, or `ask_cancel` from the
TV, stops the running one, and only the newest ask's messages reach the TV.

Tool calls report their progress with `mark(ask_id, event)`; the gaps between marks are the ask's stages in
`/stats` (`ask` section). One `ask` log line per ask: its id, source, transcript, outcome and stage times.

A spoken question (`POST /ask/audio`, `submit_audio`) is first transcribed on this server (`stt` stage), then goes
the same way as a typed one with `source="voice"`. Its audio is held in memory only and dropped once transcribed.
"""

import asyncio
import collections
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from .claude_runner import ClaudeResult
from .config import QUESTION_CHARS_MAX, Ask, Speech
from .conversation import Conversations, build_prompt
from .metrics import AskStage, Metrics
from .models import MAX_VIEWER_NAME, MAX_VIEWERS, Viewer
from .stt import AUDIO_GRACE_S, AudioTooLong, Transcriber, decode_wav, duration_s
from .tools import MAX_REPLY, _clip
from .tvlink import AskErrorCode, ScreenContext

if TYPE_CHECKING:
    import numpy

log = logging.getLogger("lumitv.ask")

ASK_ID_PATTERN = r"^[A-Za-z0-9_-]{8,40}$"
CANCEL_MEMORY_S = 60.0  # an ask_cancel may arrive before its POST /ask; such an ask is refused this long
ASKS_REMEMBERED = 64  # ask ids already taken: a repeated POST of one is ignored
MIN_LETTERS = 3

AskEvent = Literal["find_start", "find_end", "show_start", "show_end", "opened"]
# (stage, from mark, to mark); "spawn"/"exit" are when Claude Code was asked and when it answered
_STAGES: tuple[tuple[AskStage, str, str], ...] = (
    ("stt", "stt_start", "stt_end"),
    ("claude_start", "spawn", "find_start"),
    ("find", "find_start", "find_end"),
    ("think", "find_end", "show_start"),
    ("show", "show_start", "show_end"),
    ("claude_total", "spawn", "exit"),
)


class AskRequest(BaseModel):
    """`POST /ask`'s body (lumi-tv/protocol.md). A key not listed here is refused (400 naming it): a misspelt field
    from a TV build must not pass unnoticed."""

    model_config = ConfigDict(extra="forbid")

    ask_id: str = Field(pattern=ASK_ID_PATTERN)
    text: str = Field(max_length=QUESTION_CHARS_MAX)  # the configured limit is checked by AskService.too_long
    source: Literal["voice", "typed"]
    answer_id: str | None = None  # the answer on screen when the mic was pressed
    focused_token: str | None = None  # its focused poster
    # who is watching (names chosen on the TV); unknown names are ignored
    viewers: list[Annotated[str, Field(max_length=MAX_VIEWER_NAME)]] = Field(
        default_factory=list, max_length=MAX_VIEWERS
    )


_TAGS = re.compile(r"\[[^\]]*\]|\([^)]*\)")  # [BLANK_AUDIO], (music), …


def _words(text: str) -> str:
    """Lower-case words only: tags, punctuation and extra spaces dropped."""
    text = _TAGS.sub(" ", text)
    return " ".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


def usable(text: str, ignore: list[str], source: str = "voice") -> bool:
    """Whether a question is worth asking Claude.

    Spoken (`source="voice"`: a transcript, or the TV recognizer's text): at least three letters once tags and
    punctuation are gone, and not one of the `ignore` phrases (speech-to-text's hallucinations on noise).
    Typed: deliberate, so never dropped as noise; anything with a letter or digit goes ("Up", "no", "2", "thank you").
    """
    if source != "voice":
        return any(c.isalnum() for c in text)
    words = _words(text)
    if sum(c.isalpha() for c in words) < MIN_LETTERS:
        return False
    return words not in {_words(p) for p in ignore}


class HouseholdUnavailable(ToolError):
    """The TV named viewers but the household could not be read. A child it named would lose the kids limit if the
    ask ran as if nobody were named, so the ask fails instead: as a tool error Claude can read (find_titles), or
    before Claude runs (`_run`)."""


class AskHub(Protocol):
    def heard(self, ask_id: str, text: str) -> None: ...

    def ask_error(self, ask_id: str, code: AskErrorCode) -> None: ...

    def ask_reply(self, ask_id: str, text: str) -> None: ...

    def answer_context(self, answer_id: str | None, focused_token: str | None) -> ScreenContext | None: ...


class AskRunner(Protocol):
    async def run(self, prompt: str, session_id: str, resume: bool, ask_id: str) -> ClaudeResult: ...

    async def cancel(self, ask_id: str) -> None: ...


@dataclass(eq=False)
class _Ask:
    req: AskRequest
    marks: dict[str, float] = field(default_factory=dict)
    session_new: bool | None = None
    outcome: str | None = None
    task: asyncio.Task | None = None
    audio: "numpy.ndarray | None" = None  # a spoken question until it is transcribed
    transcript: str | None = None
    extra: dict[str, int | float] = field(default_factory=dict)  # logged as is: audio size, Claude's own API time

    @property
    def ask_id(self) -> str:
        return self.req.ask_id

    @property
    def question(self) -> str:
        return (self.transcript if self.transcript is not None else self.req.text).strip()


class AskService:
    def __init__(
        self,
        hub: AskHub,
        runner: AskRunner,
        conversations: Conversations,
        metrics: Metrics,
        ignore_phrases: list[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        transcriber: Transcriber | None = None,
        stt_timeout_s: float = Speech.model_fields["timeout_s"].default,
        max_audio_s: float = Speech.model_fields["max_audio_s"].default,
        max_question: int = Ask.model_fields["max_question_chars"].default,
        household: Callable[[], list[Viewer]] = lambda: [],
    ):
        """The limits default to [speech] and [ask]'s own defaults (one place: config.py)."""
        self._hub = hub
        self._runner = runner
        self._conversations = conversations
        self._metrics = metrics
        self._ignore = list(ignore_phrases or [])
        self._clock = clock
        self._transcriber = transcriber
        self._stt_timeout_s = stt_timeout_s
        self._max_audio_s = max_audio_s
        self._max_question = max_question
        self._household = household
        self._last: tuple[str, float] | None = None  # outcome of the last ask that ran to an end, and when
        self._current: _Ask | None = None
        self._tasks: set[asyncio.Task] = set()
        self._taken: collections.deque[str] = collections.deque(maxlen=ASKS_REMEMBERED)
        self._cancelled: dict[str, float] = {}  # ask id -> when the TV cancelled it

    # --- public ---------------------------------------------------------------------------------------------

    @property
    def hears_audio(self) -> bool:
        """Whether spoken questions can be transcribed here ([speech] engine)."""
        return self._transcriber is not None

    def too_long(self, req: AskRequest) -> bool:
        """The question is over [ask] max_question_chars (`POST /ask` answers 400)."""
        return len(req.text) > self._max_question

    def last_outcome(self) -> dict | None:
        """The last ask that ran to an end (not cancelled or superseded): its outcome and how long ago, for `/stats`
        (`ask.last`) and `lumitv doctor`. None before the first one."""
        if self._last is None:
            return None
        outcome, at = self._last
        return {"outcome": outcome, "age_s": round(self._clock() - at)}

    def submit(self, req: AskRequest) -> bool:
        """Start an ask and return at once. A running ask is superseded (stopped, nothing more of it reaches the TV).
        An ask the TV already cancelled, or a repeated POST of one already taken, does nothing: False (True when the
        ask was scheduled to run)."""
        return self._start(_Ask(req))

    def watching(self, ask_id: str | None) -> list[Viewer]:
        """Who is watching for ask `ask_id` (the household's viewers it named), [] when unknown or not running.
        Raises HouseholdUnavailable when the TV named viewers but the household could not be read."""
        ask = self._current
        if not ask_id or ask is None or ask.ask_id != ask_id or not ask.req.viewers:
            return []
        try:
            by_name = {v.name.lower(): v for v in self._household()}
        except Exception as e:
            log.exception("ask %s: household unavailable; failing closed", ask_id)
            raise HouseholdUnavailable("household unavailable; try again") from e
        names = [n.strip().lower() for n in ask.req.viewers]
        ask.extra["viewers_unknown"] = sum(n not in by_name for n in names)  # a renamed child shows up in the log
        return [by_name[n] for n in names if n in by_name]

    def submit_audio(
        self, ask_id: str, wav: bytes, answer_id: str | None = None, focused_token: str | None = None,
        upload_ms: float | None = None, viewers: list[str] | None = None,
    ) -> bool:  # fmt: skip
        """Start a spoken ask and return at once: transcribed here, then as `submit` with `source="voice"` (and the
        same True/False). Raises ValueError for anything but 16 kHz mono 16-bit WAV, AudioTooLong past [speech]
        max_audio_s (and pydantic's ValidationError for a bad `ask_id`). The audio is kept in memory only."""
        req = AskRequest(
            ask_id=ask_id,
            text="",
            source="voice",
            answer_id=answer_id,
            focused_token=focused_token,
            viewers=viewers or [],
        )
        audio = decode_wav(wav)
        if duration_s(audio) > self._max_audio_s + AUDIO_GRACE_S:
            raise AudioTooLong(f"{duration_s(audio):.1f} s is over {self._max_audio_s:g} s")
        ask = _Ask(req, audio=audio)
        ask.extra.update(audio_kb=round(len(wav) / 1024), audio_s=round(duration_s(audio), 1))
        if upload_ms is not None:
            ask.extra["upload_ms"] = round(upload_ms)
        return self._start(ask)

    def _start(self, ask: _Ask) -> bool:
        req = ask.req
        if req.ask_id in self._taken:
            return False
        self._taken.append(req.ask_id)
        if self._was_cancelled(req.ask_id):
            ask.outcome = "cancelled"
            self._log(ask)
            return False
        if self._current is not None:
            self._stop(self._current, "superseded")
        self._current = ask
        ask.task = asyncio.get_running_loop().create_task(self._run(ask))
        self._tasks.add(ask.task)
        ask.task.add_done_callback(lambda task: self._done(ask, task))
        return True

    async def cancel(self, ask_id: str) -> None:
        """The TV gave up on an ask (BACK, a new press). Also remembered for an ask whose POST hasn't arrived yet."""
        self._cancelled[ask_id] = self._clock()
        if self._current is not None and self._current.ask_id == ask_id:
            self._stop(self._current, "cancelled")
            await self._runner.cancel(ask_id)

    def mark(self, ask_id: str, event: AskEvent) -> None:
        """A tool call of ask `ask_id` reached `event`. Ignored unless that is the running ask. A `*_start` keeps its
        first time, the other events their latest."""
        ask = self._current
        if ask is None or ask.ask_id != ask_id:
            return
        if event.endswith("_start"):
            ask.marks.setdefault(event, self._clock())
        else:
            ask.marks[event] = self._clock()

    def on_timing(self, ask_id: str, timings: dict[str, int]) -> None:
        """Stage times the TV measured (listen, heard, posters, typed_posters)."""
        for stage, ms in timings.items():
            self._metrics.record_ask(ask_id, stage, ms)  # type: ignore[arg-type]
        self._metrics.log("ask_timing", ask_id=ask_id, **{f"{stage}_ms": ms for stage, ms in timings.items()})

    async def wait_idle(self) -> None:
        """Until no ask is running (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
            await asyncio.sleep(0)  # let done-callbacks run

    async def close(self) -> None:
        """Server shutdown: stop the running ask."""
        if self._current is not None:
            self._stop(self._current, "cancelled")
        await self.wait_idle()

    # --- one ask --------------------------------------------------------------------------------------------

    async def _run(self, ask: _Ask) -> None:
        if ask.audio is not None and not await self._transcribe(ask):
            return
        if not usable(ask.question, self._ignore, ask.req.source):
            self._hub.ask_error(ask.ask_id, "not_heard")
            ask.outcome = "not_heard"
            return
        self._hub.heard(ask.ask_id, ask.question)
        screen = self._hub.answer_context(ask.req.answer_id, ask.req.focused_token)
        try:
            watching = self.watching(ask.ask_id)
        except HouseholdUnavailable:  # logged there; the TV named viewers, so the ask must not run blind
            self._hub.ask_error(ask.ask_id, "failed")
            ask.outcome = "failed"
            return
        prompt = build_prompt(ask.question, ask.req.source, screen, watching)
        ask.marks["spawn"] = self._clock()
        result = await self._ask_claude(ask, prompt)
        ask.marks["exit"] = self._clock()
        if not result.ok:
            code: AskErrorCode = "failed" if result.error in (None, "no_session") else result.error  # type: ignore[assignment]
            self._hub.ask_error(ask.ask_id, code)
            ask.outcome = code
        elif "show_end" in ask.marks:  # the posters are up; Claude's words are for nobody
            ask.outcome = "shown"
        elif result.text:
            self._hub.ask_reply(ask.ask_id, _clip(result.text, MAX_REPLY))
            ask.outcome = "reply"
        else:
            self._hub.ask_error(ask.ask_id, "failed")
            ask.outcome = "failed"

    async def _transcribe(self, ask: _Ask) -> bool:
        """The spoken question to `ask.transcript`; False (and the TV told `failed`) when that went wrong."""
        audio, ask.audio = ask.audio, None  # dropped here, whatever happens
        ask.marks["stt_start"] = self._clock()
        try:
            if self._transcriber is None:
                raise RuntimeError("no speech-to-text engine")
            text = (
                await asyncio.wait_for(self._transcriber.transcribe(audio), self._stt_timeout_s) if len(audio) else ""
            )
        except TimeoutError:
            outcome = "stt_timeout"
        except Exception:
            log.exception("ask %s: speech-to-text failed", ask.ask_id)
            outcome = "stt_failed"
        else:
            ask.marks["stt_end"] = self._clock()
            ask.transcript = text[: self._max_question]
            return True
        self._hub.ask_error(ask.ask_id, "failed")
        ask.outcome = outcome
        return False

    async def _ask_claude(self, ask: _Ask, prompt: str) -> ClaudeResult:
        """One run in the current conversation; when Claude Code no longer knows that conversation, once more in a
        new one. A superseded/cancelled ask leaves the conversation as it is (the next ask continues it)."""
        for attempt in range(2):
            session_id, resume = self._conversations.begin()
            ask.session_new = attempt > 0 or not resume
            result = await self._runner.run(prompt, session_id, resume, ask.ask_id)
            if result.api_ms is not None:
                ask.extra["claude_api_ms"] = round(result.api_ms)
            if result.turns is not None:
                ask.extra["claude_turns"] = result.turns
            self._conversations.finish(session_id, result.ok, result.error)
            if result.error != "no_session":
                break
            log.info("ask %s: Claude Code lost the conversation; starting a new one", ask.ask_id)
        return result

    def _stop(self, ask: _Ask, outcome: str) -> None:
        if ask.outcome is None:
            ask.outcome = outcome
        if self._current is ask:
            self._current = None
        if ask.task is not None and not ask.task.done():
            ask.task.cancel()
            if outcome == "superseded":  # cancel() awaits it itself
                task = asyncio.get_running_loop().create_task(self._runner.cancel(ask.ask_id))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    def _done(self, ask: _Ask, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        ask.audio = None  # an ask stopped before it was transcribed
        if self._current is ask:
            self._current = None
        if not task.cancelled() and task.exception() is not None:
            log.error("ask %s failed", ask.ask_id, exc_info=task.exception())
            if ask.outcome is None:
                self._hub.ask_error(ask.ask_id, "failed")
                ask.outcome = "failed"
        if ask.outcome is None:
            ask.outcome = "cancelled"
        self._log(ask)

    def _log(self, ask: _Ask) -> None:
        if ask.outcome not in ("cancelled", "superseded"):
            self._last = (ask.outcome or "failed", self._clock())
        stages: dict[str, int] = {}
        for stage, start, end in _STAGES:
            a, b = ask.marks.get(start), ask.marks.get(end)
            if a is not None and b is not None and b >= a:
                ms = round((b - a) * 1000)
                stages[f"{stage}_ms"] = ms
                self._metrics.record_ask(ask.ask_id, stage, ms)
        self._metrics.log(
            "ask", ask_id=ask.ask_id, source=ask.req.source, question=ask.question, session_new=ask.session_new,
            outcome=ask.outcome, **stages, **ask.extra,
        )  # fmt: skip

    def _was_cancelled(self, ask_id: str) -> bool:
        now = self._clock()
        for old in [k for k, at in self._cancelled.items() if now - at > CANCEL_MEMORY_S]:
            del self._cancelled[old]
        return ask_id in self._cancelled
