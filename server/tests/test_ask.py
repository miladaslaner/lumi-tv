"""AskService: one question asked on the TV, answered by Claude Code (fakes: hub, runner, clock)."""

import asyncio
import json
import sqlite3

import pytest
from pydantic import ValidationError

from lumitv.ask import AskRequest, AskService, usable
from lumitv.claude_runner import ClaudeResult
from lumitv.conversation import Conversations
from lumitv.metrics import Metrics
from lumitv.tvlink import ScreenContext


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeHub:
    def __init__(self, contexts=None):
        self.sent: list[tuple] = []
        self.contexts = contexts or {}

    def heard(self, ask_id, text):
        self.sent.append(("heard", ask_id, text))

    def ask_error(self, ask_id, code):
        self.sent.append(("ask_error", ask_id, code))

    def ask_reply(self, ask_id, text):
        self.sent.append(("ask_reply", ask_id, text))

    def answer_context(self, answer_id, focused_token):
        ctx = self.contexts.get(answer_id)
        if ctx is None:
            return None
        title = dict((tok, t) for tok, t in ctx["tokens"].items()).get(focused_token)
        return ScreenContext(ctx["headline"], ctx["titles"], title)


class FakeRunner:
    """`script(call) -> ClaudeResult`, may be async and may call service.mark to simulate tool calls."""

    def __init__(self, hub=None, results=None):
        self.hub = hub
        self.calls: list[dict] = []
        self.cancelled: list[str] = []
        self.results = list(results or [])
        self.script = None
        self.blockers: dict[str, asyncio.Event] = {}

    async def run(self, prompt, session_id, resume, ask_id):
        call = {"prompt": prompt, "session_id": session_id, "resume": resume, "ask_id": ask_id}
        self.calls.append(call)
        if self.hub is not None:
            call["sent_before"] = list(self.hub.sent)
        if ask_id in self.blockers:
            await self.blockers[ask_id].wait()
        if self.script:
            r = self.script(call)
            if asyncio.iscoroutine(r):
                r = await r
            return r
        return self.results.pop(0) if self.results else ok("fine")

    async def cancel(self, ask_id):
        self.cancelled.append(ask_id)
        if ask_id in self.blockers:
            self.blockers[ask_id].set()


def ok(text):
    return ClaudeResult(True, text, None, 10.0)


def err(code):
    return ClaudeResult(False, "", code, 10.0)


class Rig:
    def __init__(self, tmp_path, contexts=None, ignore=(), transcriber=None, stt_timeout_s=10.0, household=lambda: []):
        self.clock = Clock()
        self.hub = FakeHub(contexts)
        self.runner = FakeRunner(self.hub)
        ids = iter(f"session-{i}" for i in range(1, 100))
        self.conversations = Conversations(600, self.clock, new_session_id=lambda: next(ids))
        self.log_path = tmp_path / "log.jsonl"
        self.metrics = Metrics(self.log_path, secrets=["the-server-token"])
        self.service = AskService(
            self.hub, self.runner, self.conversations, self.metrics, list(ignore), clock=self.clock,
            transcriber=transcriber, stt_timeout_s=stt_timeout_s, household=household,
        )  # fmt: skip

    def logs(self, event="ask"):
        self.metrics.close()
        lines = [json.loads(x) for x in self.log_path.read_text(encoding="utf-8").splitlines()]
        return [x for x in lines if x["event"] == event]

    async def ask(self, text, ask_id="ask-0001", source="typed", **kw):
        self.service.submit(AskRequest(ask_id=ask_id, text=text, source=source, **kw))
        await self.settle()

    async def settle(self):
        await self.service.wait_idle()


@pytest.fixture
def make_rig(tmp_path):
    made = []

    def make(**kw):
        made.append(Rig(tmp_path, **kw))
        return made[-1]

    yield make
    for r in made:
        r.metrics.close()


@pytest.fixture
def rig(make_rig):
    return make_rig()


# ---- request + noise ----


def test_request_validation():
    AskRequest(ask_id="abcd1234", text="x" * 200, source="voice")
    for bad in (
        {"ask_id": "short", "text": "hi", "source": "typed"},
        {"ask_id": "abcd 1234", "text": "hi", "source": "typed"},
        {"ask_id": "abcd1234", "text": "x" * 1001, "source": "typed"},  # [ask] max_question_chars' ceiling
        {"ask_id": "abcd1234", "text": "hi", "source": "shout"},
        {"ask_id": "abcd1234", "text": "hi", "source": "typed", "focused_tokn": "t"},  # a misspelt field is refused
    ):
        with pytest.raises(ValidationError):
            AskRequest(**bad)
    # every key the TV sends (protocol.md: the POST /ask row and the viewers note)
    AskRequest(ask_id="abcd1234", text="hi", source="voice", answer_id="a", focused_token="t", viewers=["Emma"])


def test_usable_rule():
    assert usable("something funny", [])
    assert usable("Up", []) is False
    assert usable("[MUSIC] (applause) ...", []) is False
    assert usable("Thank you.", ["thank you"]) is False
    assert usable("thank you for the film tips", ["thank you"])
    assert usable("THANK YOU!!", ["Thank you."]) is False
    assert usable("Up", [], "typed") and usable("thank you", ["thank you"], "typed")
    assert usable("...", [], "typed") is False


@pytest.mark.parametrize("text", ["", "  ", "uh", "[BLANK_AUDIO]"])
async def test_noise_is_not_heard_without_running_claude(rig, text):
    await rig.ask(text, source="voice")
    assert rig.hub.sent == [("ask_error", "ask-0001", "not_heard")]
    assert rig.runner.calls == []
    assert rig.logs()[0]["outcome"] == "not_heard"


async def test_ignore_phrases_are_not_heard(make_rig):
    rig = make_rig(ignore=["Thank you."])
    await rig.ask("thank you", source="voice")
    assert rig.hub.sent == [("ask_error", "ask-0001", "not_heard")] and rig.runner.calls == []


async def test_typed_questions_are_never_dropped_as_noise(make_rig):
    """The noise rules are for speech: typed text is deliberate ("Up" is a film, "no" answers Claude's question)."""
    for text in ("thank you", "Up", "no", "2"):
        rig = make_rig(ignore=["thank you", "you"])
        await rig.ask(text, source="typed")
        assert rig.runner.calls and rig.hub.sent[0] == ("heard", "ask-0001", text), text


async def test_spoken_thank_you_is_not_heard_but_typed_reaches_claude(make_rig):
    rig = make_rig(ignore=["thank you"])
    await rig.ask("Thank you.", ask_id="ask-voice1", source="voice")
    await rig.ask("thank you", ask_id="ask-typed1", source="typed")
    assert ("ask_error", "ask-voice1", "not_heard") in rig.hub.sent
    assert [c["ask_id"] for c in rig.runner.calls] == ["ask-typed1"]


async def test_typed_text_without_letters_or_digits_is_not_heard(rig):
    await rig.ask(" ?! ", source="typed")
    assert rig.hub.sent == [("ask_error", "ask-0001", "not_heard")] and rig.runner.calls == []


# ---- the flow ----


async def test_heard_sent_before_claude_runs(rig):
    await rig.ask("  something funny  ")
    assert rig.runner.calls[0]["sent_before"] == [("heard", "ask-0001", "something funny")]
    assert rig.runner.calls[0]["prompt"] == "The user typed: something funny"


async def test_reply_without_show_goes_to_tv(rig):
    rig.runner.results = [ok("w" * 200)]
    await rig.ask("what do we like?")
    kind, ask_id, text = rig.hub.sent[-1]
    assert (kind, ask_id) == ("ask_reply", "ask-0001") and len(text) == 140
    assert rig.logs()[0]["outcome"] == "reply"


async def test_show_during_ask_means_no_reply(rig):
    def script(call):
        rig.service.mark(call["ask_id"], "show_start")
        rig.service.mark(call["ask_id"], "show_end")
        return ok("Here are three.")

    rig.runner.script = script
    await rig.ask("something funny")
    assert rig.hub.sent == [("heard", "ask-0001", "something funny")]
    assert rig.logs()[0]["outcome"] == "shown"


async def test_empty_reply_without_show_is_failed(rig):
    rig.runner.results = [ok("")]
    await rig.ask("something funny")
    assert rig.hub.sent[-1] == ("ask_error", "ask-0001", "failed")


@pytest.mark.parametrize("code", ["login", "limit", "not_installed", "timeout", "failed"])
async def test_error_codes_forwarded(rig, code):
    rig.runner.results = [err(code)]
    await rig.ask("something funny")
    assert rig.hub.sent[-1] == ("ask_error", "ask-0001", code)
    assert len(rig.runner.calls) == 1
    assert rig.logs()[0]["outcome"] == code


async def test_last_outcome_kept_for_doctor(rig):
    assert rig.service.last_outcome() is None
    rig.runner.results = [err("login")]
    await rig.ask("something funny")
    assert rig.service.last_outcome() == {"outcome": "login", "age_s": 0}
    rig.clock.t += 90
    assert rig.service.last_outcome() == {"outcome": "login", "age_s": 90}
    await rig.ask("something else", ask_id="ask-0002")
    assert rig.service.last_outcome() == {"outcome": "reply", "age_s": 0}


async def test_cancelled_ask_does_not_hide_the_last_outcome(rig):
    rig.runner.results = [err("limit")]
    await rig.ask("something funny")
    rig.runner.blockers["ask-0002"] = asyncio.Event()
    rig.service.submit(AskRequest(ask_id="ask-0002", text="next", source="typed"))
    await asyncio.sleep(0)
    await rig.service.cancel("ask-0002")
    await rig.settle()
    assert rig.service.last_outcome()["outcome"] == "limit"


def test_too_long_uses_the_configured_limit(make_rig):
    rig = make_rig()
    assert not rig.service.too_long(AskRequest(ask_id="abcd1234", text="x" * 200, source="typed"))
    assert rig.service.too_long(AskRequest(ask_id="abcd1234", text="x" * 201, source="typed"))


async def test_no_session_retries_once_as_new(rig):
    await rig.ask("first", ask_id="ask-0001")  # a confirmed conversation
    rig.runner.results = [err("no_session"), ok("hi")]
    await rig.ask("second", ask_id="ask-0002")
    first, second = rig.runner.calls[1:]
    assert first["resume"] is True and first["session_id"] == "session-1"
    assert second["resume"] is False and second["session_id"] == "session-2"
    assert rig.hub.sent[-1] == ("ask_reply", "ask-0002", "hi")
    assert rig.logs()[1]["session_new"] is True


async def test_no_session_twice_is_failed(rig):
    rig.runner.results = [err("no_session"), err("no_session")]
    await rig.ask("something")
    assert len(rig.runner.calls) == 2
    assert rig.hub.sent[-1] == ("ask_error", "ask-0001", "failed")


async def test_followup_resumes_the_conversation(rig):
    await rig.ask("first", ask_id="ask-0001")
    await rig.ask("more like that", ask_id="ask-0002")
    a, b = rig.runner.calls
    assert a["session_id"] == b["session_id"] == "session-1"
    assert (a["resume"], b["resume"]) == (False, True)
    assert [x["session_new"] for x in rig.logs()] == [True, False]


# ---- newest ask wins ----


async def test_new_ask_cancels_previous(rig):
    rig.runner.blockers["ask-0001"] = asyncio.Event()
    rig.service.submit(AskRequest(ask_id="ask-0001", text="first question", source="voice"))
    await asyncio.sleep(0.01)
    assert rig.runner.calls[0]["ask_id"] == "ask-0001"
    rig.runner.results = [ok("second answer")]
    rig.service.submit(AskRequest(ask_id="ask-0002", text="second question", source="voice"))
    await rig.settle()
    assert "ask-0001" in rig.runner.cancelled
    assert [m for m in rig.hub.sent if m[1] == "ask-0001"] == [("heard", "ask-0001", "first question")]
    assert rig.hub.sent[-2:] == [("heard", "ask-0002", "second question"), ("ask_reply", "ask-0002", "second answer")]
    outcomes = {x["ask_id"]: x["outcome"] for x in rig.logs()}
    assert outcomes == {"ask-0001": "superseded", "ask-0002": "reply"}


async def test_superseded_before_it_started_is_logged(rig):
    rig.service.submit(AskRequest(ask_id="ask-0001", text="first question", source="voice"))
    rig.service.submit(AskRequest(ask_id="ask-0002", text="second question", source="voice"))
    await rig.settle()
    assert [c["ask_id"] for c in rig.runner.calls] == ["ask-0002"]
    assert {x["ask_id"]: x["outcome"] for x in rig.logs()} == {"ask-0001": "superseded", "ask-0002": "reply"}


async def test_cancel_from_tv_kills_run(rig):
    rig.runner.blockers["ask-0001"] = asyncio.Event()
    rig.service.submit(AskRequest(ask_id="ask-0001", text="first question", source="voice"))
    await asyncio.sleep(0.01)
    await rig.service.cancel("ask-0001")
    await rig.settle()
    assert rig.runner.cancelled == ["ask-0001"]
    assert rig.hub.sent == [("heard", "ask-0001", "first question")]  # nothing after the cancel
    assert rig.logs()[0]["outcome"] == "cancelled"


async def test_cancel_before_the_post_arrives_stops_the_ask(rig):
    await rig.service.cancel("ask-0001")  # the TV cancelled while its POST was in flight
    await rig.ask("first question", ask_id="ask-0001")
    assert rig.runner.calls == [] and rig.hub.sent == []
    assert rig.logs()[0]["outcome"] == "cancelled"


async def test_early_cancel_is_forgotten_after_a_minute(rig):
    await rig.service.cancel("ask-0001")
    rig.clock.t += 61
    await rig.ask("first question", ask_id="ask-0001")
    assert len(rig.runner.calls) == 1


async def test_cancel_of_unknown_ask_leaves_current_alone(rig):
    rig.runner.blockers["ask-0001"] = asyncio.Event()
    rig.service.submit(AskRequest(ask_id="ask-0001", text="first question", source="voice"))
    await asyncio.sleep(0.01)
    await rig.service.cancel("ask-9999")
    rig.runner.blockers["ask-0001"].set()
    await rig.settle()
    assert rig.hub.sent[-1] == ("ask_reply", "ask-0001", "fine")


async def test_repeated_post_of_same_ask_runs_once(rig):
    rig.service.submit(AskRequest(ask_id="ask-0001", text="first question", source="voice"))
    rig.service.submit(AskRequest(ask_id="ask-0001", text="first question", source="voice"))
    await rig.settle()
    await rig.ask("first question", ask_id="ask-0001")
    assert len(rig.runner.calls) == 1


async def test_submit_says_whether_the_ask_was_scheduled(make_rig):
    """The routes count an ask against [ask] per_day only when this is True."""
    rig = make_rig(transcriber=FakeTranscriber())
    first = AskRequest(ask_id="ask-0001", text="first", source="typed")
    assert rig.service.submit(first) is True
    assert rig.service.submit(first) is False  # a repeated POST
    await rig.service.cancel("ask-0002")
    assert rig.service.submit(AskRequest(ask_id="ask-0002", text="second", source="typed")) is False  # cancelled early
    assert rig.service.submit_audio("ask-0003", speech_wav()) is True
    assert rig.service.submit_audio("ask-0003", speech_wav()) is False
    await rig.settle()
    assert [c["ask_id"] for c in rig.runner.calls] == ["ask-0003"]  # ask-0001 was superseded by the newer one


# ---- screen context, timings, log ----


async def test_screen_context_seeds_prompt(make_rig):
    contexts = {
        "ans-1": {
            "headline": "Three funny films",
            "titles": [("Paddington", "movie", 116149), ("Bluey", "tv", 82728)],
            "tokens": {"tok-a": "Paddington", "tok-b": "Bluey"},
        }
    }
    rig = make_rig(contexts=contexts)
    await rig.ask("more like the second", answer_id="ans-1", focused_token="tok-b", source="voice")
    first, second = rig.runner.calls[0]["prompt"].split("\n")
    assert first == 'On the TV now: "Three funny films": Paddington (movie:116149), Bluey (tv:82728). Focused: Bluey.'
    assert second.endswith("more like the second")


async def test_stage_timings_recorded(rig):
    def script(call):
        for dt, event in ((1.5, "find_start"), (0.4, "find_end"), (1.0, "show_start"), (0.2, "show_end")):
            rig.clock.t += dt
            rig.service.mark(call["ask_id"], event)
        rig.clock.t += 0.3
        return ok("done")

    rig.runner.script = script
    await rig.ask("something funny")
    ask = rig.metrics.stats()["ask"]
    assert ask["claude_start"]["p50"] == 1500
    assert ask["find"]["p50"] == 400
    assert ask["think"]["p50"] == 1000
    assert ask["show"]["p50"] == 200
    assert ask["claude_total"]["p50"] == 3400
    line = rig.logs()[0]
    assert line["claude_start_ms"] == 1500 and line["claude_total_ms"] == 3400


async def test_marks_for_other_asks_are_ignored(rig):
    def script(call):
        rig.service.mark("someone-else", "show_end")
        return ok("words")

    rig.runner.script = script
    await rig.ask("something funny")
    assert rig.hub.sent[-1] == ("ask_reply", "ask-0001", "words")


async def test_ask_timing_from_tv_recorded(rig):
    rig.service.on_timing("ask-0001", {"listen": 800, "heard": 2500, "posters": 9000})
    ask = rig.metrics.stats()["ask"]
    assert ask["listen"]["p50"] == 800 and ask["heard"]["p50"] == 2500 and ask["posters"]["p50"] == 9000


async def test_ask_log_has_transcript_not_token(rig):
    await rig.ask("something funny with the-server-token", source="voice")
    raw = rig.log_path.read_text(encoding="utf-8")
    line = rig.logs()[0]
    assert line["question"].startswith("something funny") and line["source"] == "voice"
    assert {"ask_id", "session_new", "outcome"} <= line.keys()
    assert "the-server-token" not in raw


async def test_close_cancels_running_ask(rig):
    rig.runner.blockers["ask-0001"] = asyncio.Event()
    rig.service.submit(AskRequest(ask_id="ask-0001", text="first question", source="voice"))
    await asyncio.sleep(0.01)
    await rig.service.close()
    assert rig.logs()[0]["outcome"] == "cancelled"


# --- spoken questions (POST /ask/audio) ----------------------------------------------------------------------


class FakeTranscriber:
    def __init__(self, text="something like the expanse", delay=0.0, clock=None, takes_s=0.0):
        self.text = text
        self.delay = delay
        self.clock = clock
        self.takes_s = takes_s
        self.got: list = []

    async def transcribe(self, audio):
        self.got.append(audio)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.clock is not None:
            self.clock.t += self.takes_s
        return self.text


def speech_wav(seconds=1.0, value=1000):
    from test_stt import make_wav

    return make_wav(seconds, value=value)


async def test_audio_flow_stt_then_heard_then_claude(make_rig):
    import numpy as np

    stt = FakeTranscriber()
    rig = make_rig(transcriber=stt)
    rig.service.submit_audio("ask-0001", speech_wav(), answer_id="ans-1", focused_token=None)
    await rig.settle()
    assert len(stt.got) == 1 and isinstance(stt.got[0], np.ndarray) and stt.got[0].dtype == np.float32
    call = rig.runner.calls[0]
    assert call["sent_before"] == [("heard", "ask-0001", "something like the expanse")]
    assert "something like the expanse" in call["prompt"]
    assert rig.hub.sent[-1] == ("ask_reply", "ask-0001", "fine")
    line = rig.logs()[0]
    assert line["source"] == "voice" and line["question"] == "something like the expanse"


async def test_audio_noise_is_not_heard_and_claude_never_runs(make_rig):
    for text in ("", " Thank you.", "[BLANK_AUDIO]"):
        rig = make_rig(transcriber=FakeTranscriber(text), ignore=["thank you", "thanks for watching", "you"])
        rig.service.submit_audio("ask-0001", speech_wav())
        await rig.settle()
        assert rig.hub.sent == [("ask_error", "ask-0001", "not_heard")], text
        assert rig.runner.calls == []


async def test_empty_audio_is_not_heard_without_transcribing(make_rig):
    stt = FakeTranscriber()
    rig = make_rig(transcriber=stt)
    rig.service.submit_audio("ask-0001", speech_wav(0))
    await rig.settle()
    assert stt.got == [] and rig.hub.sent == [("ask_error", "ask-0001", "not_heard")]


async def test_stt_timeout_sends_failed(make_rig):
    rig = make_rig(transcriber=FakeTranscriber(delay=5), stt_timeout_s=0.05)
    rig.service.submit_audio("ask-0001", speech_wav())
    await rig.settle()
    assert rig.hub.sent == [("ask_error", "ask-0001", "failed")]
    assert rig.runner.calls == []
    assert rig.logs()[0]["outcome"] == "stt_timeout"


async def test_stt_error_sends_failed(make_rig):
    class Broken:
        async def transcribe(self, audio):
            raise RuntimeError("model gone")

    rig = make_rig(transcriber=Broken())
    rig.service.submit_audio("ask-0001", speech_wav())
    await rig.settle()
    assert rig.hub.sent == [("ask_error", "ask-0001", "failed")]
    assert rig.logs()[0]["outcome"] == "stt_failed"


async def test_stt_stage_recorded(make_rig):
    stt = FakeTranscriber(takes_s=0.42)
    rig = make_rig(transcriber=stt)
    stt.clock = rig.clock  # transcribing takes 420 ms on the ask's clock
    rig.service.submit_audio("ask-0001", speech_wav())
    await rig.settle()
    assert rig.metrics.stats()["ask"]["stt"]["p50"] == 420
    assert rig.logs()[0]["stt_ms"] == 420


async def test_audio_is_refused_unless_16k_mono_and_short(make_rig):
    from lumitv.stt import AudioTooLong
    from test_stt import make_wav

    rig = make_rig(transcriber=FakeTranscriber())
    with pytest.raises(ValueError):
        rig.service.submit_audio("ask-0001", make_wav(rate=44100, channels=2))
    with pytest.raises(AudioTooLong):
        rig.service.submit_audio("ask-0002", speech_wav(16))
    with pytest.raises(ValidationError):
        rig.service.submit_audio("bad id!", speech_wav())
    rig.service.submit_audio("ask-0003", speech_wav(15.2))  # the TV's own 15 s stop, plus a frame or two
    await rig.settle()
    assert rig.runner.calls[0]["ask_id"] == "ask-0003"


async def test_newer_audio_ask_supersedes(make_rig):
    rig = make_rig(transcriber=FakeTranscriber(delay=0.05))
    rig.service.submit_audio("ask-0001", speech_wav())
    rig.service.submit_audio("ask-0002", speech_wav())
    await rig.settle()
    assert [c["ask_id"] for c in rig.runner.calls] == ["ask-0002"]
    assert all(m[1] == "ask-0002" for m in rig.hub.sent)


async def test_audio_never_written_to_disk(make_rig, tmp_path, monkeypatch):
    """No file is opened for writing while a spoken ask runs (through `open`, `io.open`, `os.open` or
    `Path.write_bytes`), and the working and temp directories stay as they were (both pointed at empty directories)."""
    import builtins
    import io
    import os
    import pathlib
    import tempfile

    work, tmp = tmp_path / "work", tmp_path / "tmp"
    work.mkdir()
    tmp.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    stt = FakeTranscriber()
    rig = make_rig(transcriber=stt)
    real_open, real_os_open, real_write_bytes = builtins.open, os.open, pathlib.Path.write_bytes
    written: list[str] = []

    def spy_open(file, mode="r", *a, **kw):
        if any(c in mode for c in "wax+"):
            written.append(str(file))
        return real_open(file, mode, *a, **kw)

    def spy_os_open(path, flags, *a, **kw):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
            written.append(str(path))
        return real_os_open(path, flags, *a, **kw)

    def spy_write_bytes(self, data):
        written.append(str(self))
        return real_write_bytes(self, data)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(io, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    monkeypatch.setattr(pathlib.Path, "write_bytes", spy_write_bytes)
    rig.service.submit_audio("ask-0001", speech_wav())
    await rig.settle()
    monkeypatch.undo()
    assert written == []
    assert list(work.iterdir()) == [] and list(tmp.iterdir()) == []
    assert type(stt.got[0]).__name__ == "ndarray"  # handed over in memory
    assert rig.runner.calls  # the ask did run


async def test_audio_bytes_not_in_log(make_rig):
    import base64

    rig = make_rig(transcriber=FakeTranscriber())
    wav = speech_wav(0.2, value=0x5A5A)  # a sample that is easy to spot
    rig.service.submit_audio("ask-0001", wav)
    await rig.settle()
    rig.metrics.close()
    text = rig.log_path.read_text(encoding="utf-8")
    for needle in ("RIFF", "WAVE", base64.b64encode(wav[44:80]).decode(), wav[44:60].hex(), "ZZZZ", "23130"):
        assert needle not in text, needle
    assert "something like the expanse" in text  # the transcript is logged, like a typed question


async def test_ask_line_carries_claude_api_time_turns_and_audio_size(make_rig):
    """Observability: Anthropic's own time vs. ours, tool round trips, and the upload."""
    stt = FakeTranscriber()
    rig = make_rig(transcriber=stt)
    rig.runner.results = [ClaudeResult(True, "fine", None, 9000.0, api_ms=6400.4, turns=3)]
    rig.service.submit_audio("ask-0001", speech_wav(seconds=2.0), upload_ms=812.6)
    await rig.settle()
    line = rig.logs()[0]
    assert line["claude_api_ms"] == 6400 and line["claude_turns"] == 3
    assert line["upload_ms"] == 813 and line["audio_s"] == 2.0 and line["audio_kb"] == 63  # 2 s × 32 KB/s + header


async def test_typed_ask_line_has_no_audio_fields_and_tolerates_missing_api_time(make_rig):
    rig = make_rig()
    await rig.ask("something funny")
    line = rig.logs()[0]
    assert not {"audio_kb", "audio_s", "upload_ms", "claude_api_ms", "claude_turns"} & line.keys()


async def test_an_ask_names_who_is_watching_and_unknown_names_are_dropped(tmp_path):
    from lumitv.models import Viewer

    household = [Viewer(name="Alex"), Viewer(name="Emma", age=6)]
    rig = Rig(tmp_path, household=lambda: household)
    seen = []

    def script(call):
        seen.append(rig.service.watching(call["ask_id"]))
        return ok("fine")

    rig.runner.script = script
    await rig.ask("something funny", viewers=[" emma ", "Stranger"])  # spelling and spaces don't matter
    assert rig.runner.calls[0]["prompt"].splitlines()[0] == "Watching: Emma (6)."
    assert seen == [[household[1]]]
    assert rig.service.watching("ask-0001") == []  # finished: no longer the running ask
    assert rig.logs()[0]["viewers_unknown"] == 1  # "Stranger": a renamed child would show up here
    await rig.ask("something else", ask_id="ask-0002")
    assert "viewers_unknown" not in rig.logs()[1]  # nobody named: nothing to match


# ---- the household can't be read: fail closed ----


def _locked():
    raise sqlite3.OperationalError("database is locked")


async def test_household_failure_fails_a_named_ask_before_claude_runs(tmp_path, caplog):
    """The TV named viewers (a child may be among them) and the household could not be read: the ask fails rather
    than running without the kids limit; an ask naming nobody has nothing to protect and runs."""
    rig = Rig(tmp_path, household=_locked)
    await rig.ask("something funny", viewers=["Emma"])
    assert rig.hub.sent == [("heard", "ask-0001", "something funny"), ("ask_error", "ask-0001", "failed")]
    assert rig.runner.calls == [] and rig.logs()[0]["outcome"] == "failed"
    assert "ask ask-0001: household unavailable" in caplog.text and "database is locked" in caplog.text
    await rig.ask("something funny", ask_id="ask-0002")
    assert [c["ask_id"] for c in rig.runner.calls] == ["ask-0002"]


async def test_household_failure_during_a_tool_call_raises_for_claude(tmp_path):
    from lumitv.ask import HouseholdUnavailable
    from lumitv.models import Viewer

    reads = [[Viewer(name="Emma", age=6)]]  # the prompt's read works; the one from find_titles meets a locked store
    rig = Rig(tmp_path, household=lambda: reads.pop(0) if reads else _locked())
    raised = []

    def script(call):
        try:
            rig.service.watching(call["ask_id"])
        except HouseholdUnavailable as e:
            raised.append(str(e))
        return ok("fine")

    rig.runner.script = script
    await rig.ask("something funny", viewers=["Emma"])
    assert raised == ["household unavailable; try again"]
    assert rig.runner.calls[0]["prompt"].splitlines()[0] == "Watching: Emma (6)."


async def test_household_failure_reaches_claude_as_a_readable_tool_error(tmp_path):
    """Through the real find_titles: a ToolError's text reaches the model (anything else is masked by the SDK)."""
    from lumitv.ask import HouseholdUnavailable
    from test_tools import Rig as ToolRig, det, text

    def watching(ask_id):
        raise HouseholdUnavailable("household unavailable; try again")

    rig = ToolRig(tmp_path, {1: det("One")}, ask_id_of=lambda ctx: "ask-1", watching=watching)
    r = await rig.call("find_titles", question="something funny")
    assert r.is_error and "household unavailable; try again" in text(r)
    assert rig.hub.calls == []  # no looking, no preview: the search never started
