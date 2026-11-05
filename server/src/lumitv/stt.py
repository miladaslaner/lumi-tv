"""Speech-to-text for questions spoken into the TV remote (`POST /ask/audio`).

The audio stays in memory: the WAV body is decoded to a numpy array, handed to the transcriber and dropped. Nothing
is written to disk and nothing of it is logged (the transcript is, like a typed question).

faster-whisper is optional (`uv sync --extra speech`) and imported only when `[speech] engine = "faster-whisper"`,
so the server installs and runs without it. numpy comes with that extra.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import logging
import threading
import time
import wave
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import numpy

    from .config import Speech

log = logging.getLogger("lumitv.stt")

SAMPLE_RATE = 16000
MAX_AUDIO_BYTES = 1024 * 1024  # a POST /ask/audio body; 15 s of 16 kHz mono 16-bit is ~480 KB
AUDIO_GRACE_S = 0.5  # a clip may run this much past [speech] max_audio_s (the TV's own stop is at 15 s)


class AudioTooLong(ValueError):
    """The clip runs past [speech] max_audio_s."""


def decode_wav(data: bytes) -> numpy.ndarray:
    """A WAV body as float32 samples in [-1, 1). Only 16 kHz mono 16-bit PCM; anything else raises ValueError."""
    import numpy as np

    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            if (channels, width, rate) != (1, 2, SAMPLE_RATE):
                raise ValueError(f"need 16 kHz mono 16-bit PCM, got {rate} Hz, {channels} channel(s), {8 * width}-bit")
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        raise ValueError(f"not a PCM WAV: {e}") from None
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def duration_s(audio: numpy.ndarray) -> float:
    return len(audio) / SAMPLE_RATE


class Transcriber(Protocol):
    async def transcribe(self, audio: numpy.ndarray) -> str: ...


def available() -> bool:
    """Whether faster-whisper is installed (the `speech` extra)."""
    return importlib.util.find_spec("faster_whisper") is not None


def load_model(speech: Speech) -> Any:
    """The faster-whisper model named in [speech] (downloaded into download_dir the first time). The models are
    public, so no Hugging Face token is sent: a stale login on a shared machine otherwise fails the download (401)."""
    from faster_whisper import WhisperModel

    return WhisperModel(
        speech.model, device=speech.device, compute_type=speech.compute_type, cpu_threads=speech.cpu_threads,
        download_root=str(speech.download_dir), use_auth_token=False,
    )  # fmt: skip


def transcribe_with(model: Any, audio: numpy.ndarray, speech: Speech) -> str:
    """One clip with the [speech] settings (blocking). Silence is cut by the VAD filter, so noise gives ''."""
    segments, _info = model.transcribe(audio, beam_size=speech.beam_size, language=speech.language, vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()  # segments is lazy: the work happens here


WARM_NOISE = 0.01  # amplitude of the warm-up clip: quiet noise, so the model runs (the VAD drops pure silence)


def warm_with(model: Any, speech: Speech) -> None:
    """One second of quiet noise through the model without and with the VAD, so both are paged in (blocking)."""
    import numpy as np

    noise = (np.random.default_rng(0).standard_normal(SAMPLE_RATE) * WARM_NOISE).astype(np.float32)
    for vad in (False, True):
        segments, _info = model.transcribe(noise, beam_size=1, language=speech.language, vad_filter=vad)
        for _ in segments:  # lazy: the work happens here
            pass


class WhisperTranscriber:
    """faster-whisper, loaded once (`start()` at server startup, in a thread) and kept in memory. A failed load is
    tried again by the next ask. One clip at a time, so two never compete for the CPU: a thread can't be stopped, so
    a timed-out or superseded clip runs to its end and the next clip waits for it (inside its own timeout)."""

    def __init__(self, speech: Speech):
        self._speech = speech
        self._model: Any = None
        self._loading: asyncio.Task | None = None
        self._lock = threading.Lock()
        self.on_ready: Callable[[], None] | None = None  # called on the loop once the model is loaded

    def start(self) -> None:
        """Begin loading the model in the background; startup never waits for it. Again after a failed load."""
        if self._loading is None or self.load_failed:
            self._loading = asyncio.get_running_loop().create_task(self._load())
            self._loading.add_done_callback(self._loaded)

    async def _load(self) -> None:
        self._model = await asyncio.to_thread(load_model, self._speech)

    @property
    def ready(self) -> bool:
        """The model is loaded: a clip is transcribed without first waiting 10-25 s for a load."""
        return self._model is not None

    @property
    def load_failed(self) -> bool:
        """The last load of the model failed (the next ask tries again)."""
        task = self._loading
        return task is not None and task.done() and (task.cancelled() or task.exception() is not None)

    def _loaded(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        if task.exception() is not None:
            log.error("speech-to-text model %s did not load", self._speech.model, exc_info=task.exception())
        elif self.on_ready is not None:
            self.on_ready()

    async def transcribe(self, audio: numpy.ndarray) -> str:
        self.start()
        await asyncio.shield(self._loading)  # raises the load error; a timed-out ask must not cancel the load
        return await asyncio.to_thread(self._transcribe, audio)

    def _transcribe(self, audio: numpy.ndarray) -> str:
        with self._lock:
            return transcribe_with(self._model, audio, self._speech)

    async def warm(self) -> float | None:
        """Run the model once so it is in memory when a spoken question arrives (the TV asks when its mic opens;
        a shared PC pages the model out between asks). The seconds it took, or None when skipped: the model is not
        loaded yet, or a clip holds it (that clip pages it in itself)."""
        if self._model is None:
            return None
        return await asyncio.to_thread(self._warm)

    def _warm(self) -> float | None:
        if not self._lock.acquire(blocking=False):
            return None
        try:
            started = time.perf_counter()
            warm_with(self._model, self._speech)
            return time.perf_counter() - started
        finally:
            self._lock.release()

    async def close(self) -> None:
        if self._loading is not None and not self._loading.done():
            self._loading.cancel()  # the thread finishes on its own; its model is then dropped
            await asyncio.gather(self._loading, return_exceptions=True)


def make_transcriber(speech: Speech) -> WhisperTranscriber | None:
    """The configured engine, or None for `engine = "none"` or when faster-whisper is not installed (logged)."""
    if speech.engine == "none":
        return None
    if not available():
        log.error('[speech] engine = "faster-whisper" but faster-whisper is not installed (uv sync --extra speech)')
        return None
    return WhisperTranscriber(speech)
