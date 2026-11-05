"""JSON-lines event log (daily rotation, 30 files) and the rolling per-stage timings behind /stats."""

import json
import logging
import math
import threading
from collections import OrderedDict, deque
from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Literal, get_args

Stage = Literal["find", "show", "render"]
STAGES: tuple[Stage, ...] = ("find", "show", "render")
WINDOW_TURNS = 50
# asks from the TV: listen/heard/posters/typed_posters are the TV's clock (ask_timing), the rest the server's
AskStage = Literal[
    "listen", "heard", "stt", "claude_start", "find", "think", "show", "claude_total", "posters", "typed_posters"
]
ASK_STAGES: tuple[AskStage, ...] = get_args(AskStage)
WINDOW_ASKS = 50
KEEP_FILES = 30
CACHE_WINDOW = 500  # live lookups the hit rate covers
_DROP = {"token", "authorization"}

_warn = logging.getLogger("lumitv.metrics")


def _percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile."""
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]


class Metrics:
    """`log_path=None` keeps the in-memory stats but writes no file. `secrets` are replaced by `***` in every logged
    string (None and empty entries are skipped: optional tokens)."""

    def __init__(self, log_path: Path | str | None, secrets: Iterable[str | None] = ()):
        self._secrets = [s for s in secrets if s]
        self._turns: OrderedDict[str, dict[str, list[float]]] = OrderedDict()
        self._asks: OrderedDict[str, dict[str, list[float]]] = OrderedDict()
        self._lookups: deque[bool] = deque(maxlen=CACHE_WINDOW)
        self._call: ContextVar[dict | None] = ContextVar(f"lumitv_cache_{id(self)}", default=None)
        self._lock = threading.Lock()
        self._handler: TimedRotatingFileHandler | None = None
        if log_path is not None:
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handler = TimedRotatingFileHandler(path, when="midnight", backupCount=KEEP_FILES, encoding="utf-8")

    def close(self) -> None:
        if self._handler:
            self._handler.close()

    # ---- log -------------------------------------------------------------------------------

    def _clean(self, value):
        if isinstance(value, dict):
            return {k: self._clean(v) for k, v in value.items() if str(k).lower() not in _DROP}
        if isinstance(value, (list, tuple)):
            return [self._clean(v) for v in value]
        if isinstance(value, str):
            for s in self._secrets:
                value = value.replace(s, "***")
        return value

    def log(self, event: str, *, level: str = "INFO", **fields) -> None:
        if level == "WARNING":  # event name only: details (titles, questions) stay in the file
            _warn.warning("%s", event)
        if self._handler is None:
            return
        row = {"ts": datetime.now().astimezone().isoformat(timespec="milliseconds"), "event": event, "level": level}
        row.update(self._clean(fields))
        record = logging.LogRecord("lumitv.metrics", logging.INFO, "", 0, json.dumps(row, default=str), None, None)
        with self._lock:
            self._handler.handle(record)

    # ---- stats -----------------------------------------------------------------------------

    def record_turn(self, turn_id: str, stage: Stage, ms: float) -> None:
        self._record(self._turns, WINDOW_TURNS, turn_id, stage, ms)

    def record_ask(self, ask_id: str, stage: AskStage, ms: float) -> None:
        self._record(self._asks, WINDOW_ASKS, ask_id, stage, ms)

    def _record(
        self, window: OrderedDict[str, dict[str, list[float]]], size: int, key: str, stage: str, ms: float
    ) -> None:
        with self._lock:
            entry = window.setdefault(key, {})
            entry.setdefault(stage, []).append(ms)
            window.move_to_end(key)
            while len(window) > size:
                window.popitem(last=False)

    @contextmanager
    def track_call(self):
        """Count cache outcomes made inside this block (incl. tasks it spawns); yields {"hits", "misses"}.
        Background work such as warming runs outside any block and is not counted."""
        counts = {"hits": 0, "misses": 0}
        reset = self._call.set(counts)
        try:
            yield counts
        finally:
            self._call.reset(reset)

    def cache(self, hit: bool) -> None:
        counts = self._call.get()
        if counts is None:
            return
        counts["hits" if hit else "misses"] += 1
        self._lookups.append(hit)

    def stats(self) -> dict:
        with self._lock:
            turns = list(self._turns.values())
            asks = list(self._asks.values())
        out: dict = _stage_stats(turns, STAGES)
        lookups = list(self._lookups)
        out["cache_hit_rate"] = sum(lookups) / len(lookups) if lookups else None
        out["turns"] = len(turns)
        out["ask"] = _stage_stats(asks, ASK_STAGES)
        return out


def _stage_stats(entries: list[dict[str, list[float]]], stages: tuple[str, ...]) -> dict:
    out = {}
    for stage in stages:
        values = [ms for e in entries for ms in e.get(stage, [])]
        out[stage] = (
            {"p50": _percentile(values, 0.5), "p95": _percentile(values, 0.95), "count": len(values)}
            if values
            else {"p50": None, "p95": None, "count": 0}
        )
    return out
