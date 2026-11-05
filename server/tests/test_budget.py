from datetime import date

from lumitv.budget import AskBudget


class Clock:
    def __init__(self):
        self.t, self.d = 0.0, date(2026, 11, 4)


def make(per_minute=3, per_day=5):
    c = Clock()
    return c, AskBudget(per_minute, per_day, clock=lambda: c.t, today=lambda: c.d)


def take(b: AskBudget) -> str:
    """check, and charge when it fits: what the ask routes do for an ask that runs."""
    verdict = b.check()
    if verdict == "ok":
        b.charge()
    return verdict


def test_per_minute_cap_frees_up_after_a_minute():
    c, b = make()
    assert [take(b) for _ in range(4)] == ["ok", "ok", "ok", "per_minute"]
    c.t = 59.9
    assert take(b) == "per_minute"
    c.t = 60.0
    assert take(b) == "ok"


def test_per_day_cap_resets_at_midnight_and_refusals_do_not_count():
    c, b = make(per_minute=100, per_day=2)
    assert [take(b) for _ in range(3)] == ["ok", "ok", "per_day"]
    assert b.used_today == 2
    c.d = date(2026, 11, 5)
    assert b.used_today == 0 and take(b) == "ok"


def test_check_counts_nothing_until_charged():
    """An ask that is accepted but never runs (a repeat, an early cancel) is checked, not charged."""
    c, b = make(per_minute=1, per_day=1)
    assert [b.check() for _ in range(3)] == ["ok", "ok", "ok"] and b.used_today == 0
    b.charge()
    assert b.check() == "per_day" and b.used_today == 1  # per_day is judged before per_minute
    c.d = date(2026, 11, 5)
    assert b.check() == "per_minute"  # the day reset; the minute has not passed
