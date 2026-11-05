import asyncio
import time

from lumitv.stall import StallWatch, where


async def test_a_blocked_loop_is_logged_once_with_where_it_was_stuck():
    stalls = []
    watch = StallWatch(lambda ms, stack: stalls.append((ms, stack)), threshold_s=0.15, interval_s=0.02)
    watch.start()
    try:
        await asyncio.sleep(0.1)
        _block_the_loop(0.4)
        await asyncio.sleep(0.1)
    finally:
        await watch.stop()
    assert len(stalls) == 1
    ms, stack = stalls[0]
    assert 300 <= ms <= 1500  # the 0.4 s block, plus slack for a busy machine
    assert any("_block_the_loop" in f for f in stack), stack


async def test_a_busy_but_responsive_loop_logs_nothing():
    stalls = []
    watch = StallWatch(lambda ms, stack: stalls.append(ms), threshold_s=0.15, interval_s=0.02)
    watch.start()
    try:
        for _ in range(20):
            _block_the_loop(0.01)
            await asyncio.sleep(0.01)
    finally:
        await watch.stop()
    assert stalls == []


def _block_the_loop(seconds):
    time.sleep(seconds)  # synchronous on purpose: this is what a stall looks like


def test_where_shortens_paths_and_keeps_the_innermost_frames():
    import sys

    frames = where(sys._getframe(), limit=2)
    assert len(frames) == 2 and frames[-1].endswith("test_where_shortens_paths_and_keeps_the_innermost_frames")
