"""Speech-to-text benchmark: which faster-whisper model to put in [speech] model on this machine.

    uv sync --extra speech
    uv run python scripts/bench_stt.py bench/*.wav --models tiny.en base.en small.en --threads 4

Each clip must be 16 kHz mono 16-bit WAV (what the TV sends). Per model: load time (the first run downloads it into
--download-dir), each clip's transcription time and transcript, and the p50. It uses the server's own code and
settings (lumitv.stt, VAD filter, beam size, language), so the numbers are what an ask will see. Pick the smallest
model whose transcripts are right on every clip with p50 <= 1.5 s.

Heavy while it runs (it loads models and uses --threads cores); run it outside the shared machine's quiet hours.
"""

import argparse
import glob
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lumitv import stt
from lumitv.config import Speech


def bench(
    clips: dict[str, Any], models: list[str], base: Speech, load: Callable[[Speech], Any] = stt.load_model, out=print
) -> dict[str, dict]:
    """Every model over every clip (decoded audio by name). Returns {model: {"load_s", "ms": [..], "p50_ms"}}."""
    results: dict[str, dict] = {}
    for name in models:
        speech = base.model_copy(update={"model": name})
        t0 = time.perf_counter()
        model = load(speech)
        load_s = time.perf_counter() - t0
        out(f"\n{name}: loaded in {load_s:.1f} s")
        times = []
        for clip, audio in clips.items():
            t0 = time.perf_counter()
            text = stt.transcribe_with(model, audio, speech)
            ms = (time.perf_counter() - t0) * 1000
            times.append(ms)
            out(f"  {ms:7.0f} ms  {clip}: {text!r}")
        p50 = statistics.median(times) if times else 0.0
        out(f"  p50 {p50:.0f} ms over {len(times)} clip(s)")
        results[name] = {"load_s": load_s, "ms": times, "p50_ms": p50}
        del model
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("wavs", nargs="+", help="16 kHz mono 16-bit WAV clips (globs are expanded, also on Windows)")
    p.add_argument("--models", nargs="+", default=["tiny.en", "base.en", "small.en"])
    p.add_argument("--threads", type=int, default=Speech().cpu_threads)
    p.add_argument("--device", default=Speech().device)
    p.add_argument("--compute-type", default=Speech().compute_type)
    p.add_argument("--beam-size", type=int, default=Speech().beam_size)
    p.add_argument("--language", default=Speech().language)
    p.add_argument("--download-dir", type=Path, default=Speech().download_dir)
    args = p.parse_args(argv)
    if not stt.available():
        print("faster-whisper is not installed: uv sync --extra speech", file=sys.stderr)
        return 2
    paths = [Path(x) for pattern in args.wavs for x in (sorted(glob.glob(pattern)) or [pattern])]
    clips = {}
    for path in paths:
        try:
            clips[path.name] = stt.decode_wav(path.read_bytes())
        except (OSError, ValueError) as e:
            print(f"{path}: {e}", file=sys.stderr)
            return 2
    base = Speech(
        engine="faster-whisper", device=args.device, compute_type=args.compute_type, cpu_threads=args.threads,
        beam_size=args.beam_size, language=args.language, download_dir=args.download_dir,
    )  # fmt: skip
    results = bench(clips, args.models, base)
    print("\nmodel        load s   p50 ms")
    for name, r in results.items():
        print(f"{name:<12} {r['load_s']:6.1f} {r['p50_ms']:8.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
