"""How many asks the server takes: a per-minute cap (anything on the LAN with the token could otherwise loop) and a
per-day cap (each ask is a Claude run on the household's subscription). `check` answers whether one more fits;
`charge` counts it once it is actually scheduled, so a duplicate, an early-cancelled or a refused ask costs nothing."""

import time
from collections import deque
from collections.abc import Callable
from datetime import date
from typing import Literal

Verdict = Literal["ok", "per_minute", "per_day"]


class AskBudget:
    def __init__(
        self,
        per_minute: int,
        per_day: int,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], date] = date.today,
    ):
        self._per_minute, self._per_day = per_minute, per_day
        self._clock, self._today = clock, today
        self._recent: deque[float] = deque()
        self._day, self._count = today(), 0

    def check(self) -> Verdict:
        """Whether one more ask fits both caps now. Nothing is counted."""
        now, today = self._clock(), self._today()
        if today != self._day:
            self._day, self._count = today, 0
        while self._recent and now - self._recent[0] >= 60:
            self._recent.popleft()
        if self._count >= self._per_day:
            return "per_day"
        if len(self._recent) >= self._per_minute:
            return "per_minute"
        return "ok"

    def charge(self) -> None:
        """Count one ask: after `check` said ok and the ask was scheduled to run."""
        self._recent.append(self._clock())
        self._count += 1

    @property
    def used_today(self) -> int:
        return self._count if self._today() == self._day else 0
