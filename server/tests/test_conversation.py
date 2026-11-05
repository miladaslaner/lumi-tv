from pathlib import Path

import pytest

from lumitv import config, services as services_mod
from lumitv.conversation import Conversations, build_prompt
from lumitv.tools import render_tv_instructions
from lumitv.tvlink import ScreenContext

EXAMPLE = Path(__file__).parent.parent / "lumitv.example.toml"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


def test_first_ask_starts_new_session(clock):
    c = Conversations(600, clock)
    s, resume = c.begin()
    assert s and resume is False


def test_followup_within_quiet_period_resumes(clock):
    c = Conversations(600, clock)
    s, _ = c.begin()
    c.finish(s, True, None)
    clock.t += 599
    assert c.begin() == (s, True)


def test_new_session_after_quiet_period(clock):
    c = Conversations(600, clock)
    s, _ = c.begin()
    c.finish(s, True, None)
    clock.t += 601
    s2, resume = c.begin()
    assert s2 != s and resume is False


def test_quiet_period_counts_from_last_ok_ask(clock):
    c = Conversations(600, clock)
    s, _ = c.begin()
    c.finish(s, True, None)
    clock.t += 500
    assert c.begin() == (s, True)
    c.finish(s, True, None)
    clock.t += 500
    assert c.begin() == (s, True)


def test_failed_first_run_forgets_session(clock):
    c = Conversations(600, clock)
    s, _ = c.begin()
    c.finish(s, False, "failed")
    s2, resume = c.begin()
    assert s2 != s and resume is False


def test_failed_followup_keeps_session(clock):
    c = Conversations(600, clock)
    s, _ = c.begin()
    c.finish(s, True, None)
    c.begin()
    c.finish(s, False, "timeout")
    assert c.begin() == (s, True)


def test_no_session_error_forgets_session(clock):
    c = Conversations(600, clock)
    s, _ = c.begin()
    c.finish(s, True, None)
    c.begin()
    c.finish(s, False, "no_session")
    s2, resume = c.begin()
    assert s2 != s and resume is False


def test_finish_of_an_older_session_is_ignored(clock):
    c = Conversations(600, clock)
    s, _ = c.begin()
    c.finish(s, False, "failed")
    s2, _ = c.begin()
    c.finish(s, False, "no_session")  # a late result of the forgotten session
    assert c.begin() == (s2, False)


def test_new_session_ids_come_from_the_factory(clock):
    ids = iter(["warm-1", "warm-2"])
    c = Conversations(600, clock, new_session_id=lambda: next(ids))
    assert c.begin() == ("warm-1", False)
    c.finish("warm-1", False, "failed")
    assert c.begin() == ("warm-2", False)


def test_build_prompt_with_screen_and_focus():
    screen = ScreenContext("Three cosy films", [("Paddington", "movie", 116149), ("Bluey", "tv", 82728)], "Bluey")
    assert build_prompt("not that one", "voice", screen) == (
        'On the TV now: "Three cosy films": Paddington (movie:116149), Bluey (tv:82728). Focused: Bluey.\n'
        "The user said (speech-to-text, may contain errors): not that one"
    )


def test_build_prompt_without_focus():
    screen = ScreenContext("Two picks", [("Up", "movie", 14160)], None)
    assert build_prompt("more like that", "typed", screen) == (
        'On the TV now: "Two picks": Up (movie:14160).\nThe user typed: more like that'
    )


def test_build_prompt_typed_without_screen():
    assert build_prompt("something lighter", "typed", None) == "The user typed: something lighter"


def test_tv_instructions_include_v1_and_tv_rules(tmp_path):
    (tmp_path / ".env").write_text("TMDB_TOKEN=t\nSERVER_TOKEN=s\n")
    cfg = config.load(EXAMPLE, env_file=tmp_path / ".env")
    text = render_tv_instructions(cfg, services_mod.load(cfg))
    assert "exactly two tool calls" in text
    assert "recognition errors" in text and "at most 70 characters" in text
    assert "show_on_tv" in text and "exclude_tmdb_ids" in text
    assert "{" not in text
    raw = (Path(__file__).parent.parent / "content" / "instructions_tv.md").read_text(encoding="utf-8")
    for name in ["Netflix", "Videoland", "Prime", "NordVPN", "Lumi"]:
        assert name not in raw


def test_prompt_says_who_is_watching():
    from lumitv.conversation import build_prompt
    from lumitv.models import Viewer

    p = build_prompt("something funny", "typed", None, [Viewer(name="Alex"), Viewer(name="Emma", age=6)])
    assert p.splitlines()[0] == "Watching: Alex, Emma (6)."
