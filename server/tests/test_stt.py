"""Speech-to-text: WAV decoding, the noise rule on transcripts, the faster-whisper wrapper (fake model)."""

import asyncio
import io
import threading
import time
import wave

import numpy as np
import pytest

from lumitv import stt
from lumitv.ask import usable
from lumitv.config import Speech


def make_wav(seconds=0.5, rate=16000, channels=1, width=2, value=1000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        frames = int(seconds * rate)
        sample = int(value).to_bytes(width, "little", signed=True)
        w.writeframes(sample * frames * channels)
    return buf.getvalue()


def test_decode_wav_accepts_16k_mono_s16():
    audio = stt.decode_wav(make_wav(0.5, value=16384))
    assert audio.dtype == np.float32 and audio.shape == (8000,)
    assert audio[0] == pytest.approx(0.5)
    assert stt.duration_s(audio) == pytest.approx(0.5)


def test_decode_wav_rejects_other_formats():
    with pytest.raises(ValueError, match="44100 Hz, 2 channel"):
        stt.decode_wav(make_wav(rate=44100, channels=2))
    with pytest.raises(ValueError):
        stt.decode_wav(make_wav(width=1, value=10))  # 8-bit
    with pytest.raises(ValueError):
        stt.decode_wav(make_wav(rate=8000))
    for junk in (b"", b"not a wav at all", make_wav()[:30]):
        with pytest.raises(ValueError):
            stt.decode_wav(junk)


def test_hallucinations_and_blank_not_usable():
    ignore = Speech().ignore_phrases
    for text in ("", "[BLANK_AUDIO]", "Thank you.", " you", "Thanks for watching!"):
        assert not usable(text, ignore), text
    assert usable("something like the expanse", ignore)


class FakeModel:
    def __init__(self, delay=0.0):
        self.calls: list[tuple] = []
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def transcribe(self, audio, **kw):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.calls.append((audio, kw))

        def segments():
            time.sleep(self.delay)
            with self._lock:
                self.active -= 1
            yield type("Seg", (), {"text": " something like"})()
            yield type("Seg", (), {"text": " the expanse "})()

        return segments(), None


async def test_whisper_transcriber_loads_once_and_uses_the_speech_settings(monkeypatch):
    model = FakeModel()
    loads = []
    monkeypatch.setattr(stt, "load_model", lambda speech: loads.append(speech) or model)
    speech = Speech(engine="faster-whisper", beam_size=2, language="en")
    t = stt.WhisperTranscriber(speech)
    t.start()
    audio = np.zeros(1600, dtype=np.float32)
    assert await t.transcribe(audio) == "something like the expanse"
    assert await t.transcribe(audio) == "something like the expanse"
    assert len(loads) == 1
    got, kw = model.calls[0]
    assert got is audio  # the array itself: no file in between
    assert kw == {"beam_size": 2, "language": "en", "vad_filter": True}
    await t.close()


async def test_whisper_transcriber_runs_one_clip_at_a_time(monkeypatch):
    model = FakeModel(delay=0.05)
    monkeypatch.setattr(stt, "load_model", lambda speech: model)
    t = stt.WhisperTranscriber(Speech(engine="faster-whisper"))
    audio = np.zeros(10, dtype=np.float32)
    await asyncio.gather(t.transcribe(audio), t.transcribe(audio), t.transcribe(audio))
    assert model.max_active == 1


async def test_whisper_transcriber_reports_ready_once_loaded(monkeypatch):
    """`ready` (what hello's `speech` follows) is false until the model is in; `on_ready` fires once, after a load
    that worked, never after one that failed."""
    attempts = []

    def flaky(speech):
        attempts.append(speech.model)
        if len(attempts) == 1:
            raise OSError("no model")
        return FakeModel()

    monkeypatch.setattr(stt, "load_model", flaky)
    t = stt.WhisperTranscriber(Speech(engine="faster-whisper"))
    readies = []
    t.on_ready = lambda: readies.append(True)
    assert not t.ready and not t.load_failed
    t.start()
    with pytest.raises(OSError):
        await t.transcribe(np.zeros(10, dtype=np.float32))
    assert not t.ready and t.load_failed and readies == []
    assert await t.transcribe(np.zeros(10, dtype=np.float32)) == "something like the expanse"
    await asyncio.sleep(0)  # the load task's done-callback runs after the await that woke us
    assert t.ready and readies == [True]
    await t.close()


async def test_whisper_transcriber_load_error_reaches_the_ask_and_the_next_ask_tries_again(monkeypatch):
    attempts = []

    def flaky(speech):
        attempts.append(speech.model)
        if len(attempts) < 3:
            raise OSError("no model")
        return FakeModel()

    monkeypatch.setattr(stt, "load_model", flaky)
    t = stt.WhisperTranscriber(Speech(engine="faster-whisper"))
    t.start()
    for _ in range(2):
        with pytest.raises(OSError):
            await t.transcribe(np.zeros(10, dtype=np.float32))
    assert await t.transcribe(np.zeros(10, dtype=np.float32)) == "something like the expanse"
    assert len(attempts) == 3


def test_make_transcriber(monkeypatch):
    assert stt.make_transcriber(Speech()) is None  # engine "none"
    monkeypatch.setattr(stt, "available", lambda: False)
    assert stt.make_transcriber(Speech(engine="faster-whisper")) is None
    monkeypatch.setattr(stt, "available", lambda: True)
    assert isinstance(stt.make_transcriber(Speech(engine="faster-whisper")), stt.WhisperTranscriber)


def test_speech_config_validates():
    with pytest.raises(ValueError):
        Speech(engine="whisper.cpp")
    with pytest.raises(ValueError):
        Speech(max_audio_s=0)
    with pytest.raises(ValueError):
        Speech(cpu_threads=0)


def _bench_module():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parent.parent / "scripts" / "bench_stt.py"
    spec = importlib.util.spec_from_file_location("bench_stt", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bench_stt_runs_each_model_over_each_clip():
    bench_stt = _bench_module()
    loaded = []

    def load(speech):
        loaded.append((speech.model, speech.cpu_threads))
        return FakeModel()

    clips = {"a.wav": stt.decode_wav(make_wav(0.2)), "b.wav": stt.decode_wav(make_wav(0.3))}
    lines = []
    results = bench_stt.bench(clips, ["tiny.en", "base.en"], Speech(cpu_threads=2), load=load, out=lines.append)
    assert loaded == [("tiny.en", 2), ("base.en", 2)]
    assert set(results) == {"tiny.en", "base.en"} and len(results["tiny.en"]["ms"]) == 2
    assert any("a.wav: 'something like the expanse'" in line for line in lines)
    assert any(line.strip().startswith("p50") for line in lines)


def test_bench_stt_without_faster_whisper_says_how_to_install(monkeypatch, capsys):
    bench_stt = _bench_module()
    monkeypatch.setattr(stt, "available", lambda: False)
    assert bench_stt.main(["x.wav"]) == 2
    assert "uv sync --extra speech" in capsys.readouterr().err


def test_load_model_never_sends_a_hugging_face_token(monkeypatch, tmp_path):
    """A stale HF login on a shared PC made the public model download fail with 401 (2026-11-04)."""
    import sys
    import types

    seen = {}
    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = lambda name, **kw: seen.update(kw, name=name) or "model"
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    assert stt.load_model(Speech(download_dir=tmp_path)) == "model"
    assert seen["use_auth_token"] is False and seen["name"] == "base.en"


async def test_warm_runs_the_model_on_quiet_noise_with_and_without_the_vad(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(stt, "load_model", lambda speech: model)
    t = stt.WhisperTranscriber(Speech(engine="faster-whisper", language="en"))
    assert await t.warm() is None  # not loaded yet: nothing to page in
    t.start()
    await t.transcribe(np.zeros(10, dtype=np.float32))
    model.calls.clear()
    took = await t.warm()
    assert took is not None and took >= 0
    # noise, not silence: the VAD would drop silence before the model runs, and nothing would be paged in
    (a1, kw1), (_, kw2) = model.calls
    assert a1.dtype == np.float32 and len(a1) == stt.SAMPLE_RATE and 0 < float(np.abs(a1).max()) < 0.1
    assert kw1["vad_filter"] is False and kw2["vad_filter"] is True
    await t.close()


async def test_warm_is_skipped_while_a_clip_holds_the_model(monkeypatch):
    model = FakeModel(delay=0.2)
    monkeypatch.setattr(stt, "load_model", lambda speech: model)
    t = stt.WhisperTranscriber(Speech(engine="faster-whisper"))
    t.start()
    await t.transcribe(np.zeros(10, dtype=np.float32))
    clip = asyncio.create_task(t.transcribe(np.zeros(10, dtype=np.float32)))
    await asyncio.sleep(0.05)
    assert await t.warm() is None  # the clip itself pages the model in; a warm-up would only make it wait
    await clip
    await t.close()
