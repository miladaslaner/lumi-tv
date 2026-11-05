"""Event-loop stall watchdog: logs when the server's asyncio loop stops running for longer than a threshold, with
where it was stuck (the loop thread's stack). A stalled loop delays everything at once: TV link, MCP tools, uploads.

A heartbeat task on the loop stamps the time every `interval_s`; a daemon thread checks the stamp. When it is older
than `threshold_s`, the thread captures the loop thread's current stack (the code that is blocking), and logs one
`loop_stall` event when the loop runs again, with the stall length and that stack.
"""

import asyncio
import sys
import threading
import time
import traceback
from collections.abc import Callable

MAX_FRAMES = 8  # innermost frames kept in the log line


def where(frame, limit: int = MAX_FRAMES) -> list[str]:
    """The innermost `limit` frames of a stack as "file:line function" (file names shortened to the package)."""
    out = []
    for fs in traceback.extract_stack(frame)[-limit:]:
        name = fs.filename.replace("\\", "/")
        name = name.split("/site-packages/")[-1].split("/src/")[-1]
        out.append(f"{name}:{fs.lineno} {fs.name}")
    return out


class StallWatch:
    """Start with `start()` from inside the running loop; `stop()` on shutdown. `log(ms, stack)` is called on the
    loop (not the watchdog thread) once per stall."""

    def __init__(self, log: Callable[[int, list[str]], None], threshold_s: float = 0.25, interval_s: float = 0.05):
        self._log = log
        self._threshold_s = threshold_s
        self._interval_s = interval_s
        self._beat = time.monotonic()
        self._stack: list[str] | None = None  # captured during the current stall
        self._loop_thread_id: int | None = None
        self._task: asyncio.Task | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._loop_thread_id = threading.get_ident()
        self._beat = time.monotonic()
        self._task = asyncio.get_running_loop().create_task(self._heartbeat())
        self._thread = threading.Thread(target=self._watch, name="lumitv-stall-watch", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            now = time.monotonic()
            late = now - self._beat - self._interval_s
            if late > self._threshold_s:
                self._log(round(late * 1000), self._stack or ["(not captured)"])
            self._stack = None
            self._beat = now

    def _watch(self) -> None:
        while not self._stop.wait(self._interval_s):
            if self._stack is None and time.monotonic() - self._beat > self._threshold_s + self._interval_s:
                frame = sys._current_frames().get(self._loop_thread_id or 0)
                self._stack = where(frame) if frame is not None else None
