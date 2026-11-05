import re
from datetime import datetime
from pathlib import Path

import pytest

from lumitv import config

EXAMPLE = Path(__file__).parent.parent / "lumitv.example.toml"


def _env(tmp_path, body="TMDB_TOKEN=fake-tmdb\nSERVER_TOKEN=fake-server\n"):
    p = tmp_path / ".env"
    p.write_text(body)
    return p


def test_load_example(tmp_path):
    c = config.load(EXAMPLE, env_file=_env(tmp_path))
    assert c.tmdb_token == "fake-tmdb" and c.server_token == "fake-server"
    assert c.server.port == 8765 and c.answers.picks == 3
    assert (
        c.cache.warm_at == "17:00"
        and "comedy" in c.cache.warm_genres
        and "kids" in c.cache.warm_genres
        and c.timeouts.tmdb_s == 3.0
    )
    assert [s.id for s in c.services] == ["netflix", "prime", "videoland", "youtube"]
    assert c.ratings.fallback_map["TV-MA"] == "18" and c.kids.ages["18"] == 18 and c.kids.max_age == 15
    assert c.kids_policy().ages == c.kids.ages and c.kids_policy().max_age == 15


def test_missing_token_names_key(tmp_path, monkeypatch):
    monkeypatch.delenv("SERVER_TOKEN", raising=False)
    with pytest.raises(Exception, match="SERVER_TOKEN"):
        config.load(EXAMPLE, env_file=_env(tmp_path, "TMDB_TOKEN=x\n"))


def test_invalid_toml_value_names_key(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text(EXAMPLE.read_text(encoding="utf-8").replace("port = 8765", 'port = "x"'))
    with pytest.raises(Exception, match="port"):
        config.load(bad, env_file=_env(tmp_path))


def test_candidates_default_and_validation(tmp_path):
    c = config.load(EXAMPLE, env_file=_env(tmp_path))
    assert c.answers.candidates == 20 and c.server.lookup_concurrency == 16
    bad = tmp_path / "bad.toml"
    bad.write_text(EXAMPLE.read_text(encoding="utf-8").replace("candidates = 20", "candidates = 2"))
    with pytest.raises(ValueError, match="candidates"):
        config.load(bad, env_file=_env(tmp_path))


def test_hours_candidates_default_and_validation(tmp_path):
    c = config.load(EXAMPLE, env_file=_env(tmp_path))
    assert c.answers.hours_candidates == 40
    bad = tmp_path / "bad.toml"
    bad.write_text(EXAMPLE.read_text(encoding="utf-8").replace("hours_candidates = 40", "hours_candidates = 10"))
    with pytest.raises(ValueError, match="hours_candidates"):
        config.load(bad, env_file=_env(tmp_path))


def test_ranking_defaults_in_example(tmp_path):
    c = config.load(EXAMPLE, env_file=_env(tmp_path))
    assert c.ranking.exclude_tmdb == [] and c.ranking.blocked() == set()
    assert 99 in c.ranking.off_genres_unless_liked  # Documentary
    assert (c.ranking.min_votes, c.ranking.kids_min_votes) == (300, 50)
    assert c.ranking.genre_weight > 0 and 0 <= c.ranking.discover_share <= 1 and c.ranking.liked_from_top >= 1


def test_exclude_tmdb_parses_kind_qualified_ids(tmp_path):
    good = tmp_path / "good.toml"
    good.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace("exclude_tmdb = []", 'exclude_tmdb = ["tv:5371", "movie:603"]')
    )
    c = config.load(good, env_file=_env(tmp_path))
    assert c.ranking.blocked() == {(5371, "tv"), (603, "movie")}


@pytest.mark.parametrize(
    "entry", ['"5371"', '"show:5371"', '"tv:"', '"tv:0"', '"tv:abc"', '"tv: 5371"', '"TV:5371"', "5371", '"tv:5371\\n"']
)
def test_exclude_tmdb_rejects_bad_entries(tmp_path, entry):
    bad = tmp_path / "bad.toml"
    bad.write_text(EXAMPLE.read_text(encoding="utf-8").replace("exclude_tmdb = []", f"exclude_tmdb = [{entry}]"))
    with pytest.raises(ValueError, match="exclude_tmdb"):
        config.load(bad, env_file=_env(tmp_path))


def test_old_config_without_ask_section_loads(tmp_path):
    text = re.sub(r"\n\[ask\]\n.*?(?=\n\[)", "\n", EXAMPLE.read_text(encoding="utf-8"), flags=re.S)
    text = re.sub(r"\n(listen_ms|heard_ms|ask_posters_p50_ms) = \d+", "", text)
    assert "[ask]" not in text and "ask_posters_p50_ms" not in text
    old = tmp_path / "old.toml"
    old.write_text(text)
    cfg = config.load(old, env_file=_env(tmp_path))
    assert cfg.ask.model == "claude-haiku-5-5" and cfg.ask.new_conversation_after_s == 600 and cfg.ask.effort == "low"
    assert cfg.ask.enabled and cfg.ask.claude_path == "claude" and cfg.ask.mcp_url is None
    assert cfg.ask.timeout_s == 45.0 and cfg.ask.max_question_chars == 200 and cfg.ask.workdir == Path("claude-home")
    assert (cfg.budgets.listen_ms, cfg.budgets.heard_ms, cfg.budgets.ask_posters_p50_ms) == (1000, 3000, 12000)
    assert cfg.budgets.posters_p50_ms == 8000  # v1 budget unchanged


def test_ask_section_in_example(tmp_path):
    c = config.load(EXAMPLE, env_file=_env(tmp_path))
    assert c.ask.model == "claude-haiku-5-5" and c.ask.mcp_url is None and c.budgets.ask_posters_p50_ms == 12000


def test_cold_find_budget_is_3s_by_default(tmp_path):
    assert config.load(EXAMPLE, env_file=_env(tmp_path)).budgets.find_p95_cold_ms == 3000
    older = tmp_path / "older.toml"
    older.write_text(re.sub(r"\n(find_p95_cold_ms|ask_posters_p95_ms) = .*", "", EXAMPLE.read_text(encoding="utf-8")))
    b = config.load(older, env_file=_env(tmp_path)).budgets
    assert (b.find_p95_cold_ms, b.ask_posters_p95_ms) == (3000, 15000)


def test_ask_posters_p95_budget_in_example(tmp_path):
    assert config.load(EXAMPLE, env_file=_env(tmp_path)).budgets.ask_posters_p95_ms == 15000


def test_assistant_vpn_name_defaults_to_the_vpn(tmp_path):
    """[assistant] vpn_name fills {vpn} in the TV's strings; a config from before it loads with the TV's own default."""
    text = EXAMPLE.read_text(encoding="utf-8")
    assert 'vpn_name = "the VPN"' in text
    assert config.load(EXAMPLE, env_file=_env(tmp_path)).assistant.vpn_name == "the VPN"
    old = tmp_path / "old.toml"
    old.write_text(re.sub(r"(?m)^vpn_name = .*\n", "", text))
    assert "vpn_name" not in old.read_text(encoding="utf-8")
    assert config.load(old, env_file=_env(tmp_path)).assistant.vpn_name == "the VPN"
    named = tmp_path / "named.toml"
    named.write_text(text.replace('vpn_name = "the VPN"', 'vpn_name = "NordVPN"'))
    assert config.load(named, env_file=_env(tmp_path)).assistant.vpn_name == "NordVPN"


def test_new_conversation_after_s_floor(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text(
        re.sub(r"new_conversation_after_s = \d+", "new_conversation_after_s = 30", EXAMPLE.read_text(encoding="utf-8"))
    )
    with pytest.raises(ValueError, match=r"ask\.new_conversation_after_s"):
        config.load(bad, env_file=_env(tmp_path))


@pytest.mark.parametrize(
    "old, new, key",
    [
        ("port = 8765", "port = 0", "server.port"),
        ("port = 8765", "port = 70000", "server.port"),
        ("lookup_concurrency = 16", "lookup_concurrency = 0", "server.lookup_concurrency"),
        ("timeout_s = 45.0", "timeout_s = 0", "ask.timeout_s"),
        ("timeout_s = 45.0", "timeout_s = -5", "ask.timeout_s"),
        ("max_question_chars = 200", "max_question_chars = 0", "ask.max_question_chars"),
        ("max_question_chars = 200", "max_question_chars = 1001", "ask.max_question_chars"),
        ("per_minute = 6", "per_minute = 0", "ask.per_minute"),
        ("max_audio_s = 15", "max_audio_s = 0", "speech.max_audio_s"),
        ("timeout_s = 10", "timeout_s = 0", "speech.timeout_s"),
        ("tmdb_s = 3.0", "tmdb_s = 0", "timeouts.tmdb_s"),
        ("tv_ack_s = 5.0", "tv_ack_s = -1", "timeouts.tv_ack_s"),
        ("wikidata_s = 2.0", "wikidata_s = 0", "timeouts.wikidata_s"),
    ],
)
def test_config_rejects_zero_or_negative_timeouts_and_bad_ports(tmp_path, old, new, key):
    """A typo like `timeout_s = 0` would make every ask time out (or every show return offline) with no startup error."""
    text = EXAMPLE.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    bad = tmp_path / "bad.toml"
    bad.write_text(text.replace(old, new))
    with pytest.raises(ValueError, match=re.escape(key)):
        config.load(bad, env_file=_env(tmp_path))


@pytest.mark.parametrize("value", ['"none"', '"LOW"', '""', "3"])
def test_ask_effort_rejects_unknown_levels(tmp_path, value):
    bad = tmp_path / "lumitv.toml"
    bad.write_text(EXAMPLE.read_text(encoding="utf-8").replace('effort = "low"', f"effort = {value}"))
    with pytest.raises(Exception, match="effort"):
        config.load(bad, env_file=_env(tmp_path))


# ---- [kids] / [ratings] ----------------------------------------------------------------------------------------

KIDS_EXAMPLE = re.search(r"\n\[kids\]\n.*?(?=\n\[\[services\]\])", EXAMPLE.read_text(encoding="utf-8"), re.S).group(0)
RATINGS_EXAMPLE = re.search(r"\n\[ratings\]\n.*?(?=\n\[kids\])", EXAMPLE.read_text(encoding="utf-8"), re.S).group(0)


def _with_sections(tmp_path, kids=KIDS_EXAMPLE, ratings=RATINGS_EXAMPLE):
    text = EXAMPLE.read_text(encoding="utf-8").replace(KIDS_EXAMPLE, kids).replace(RATINGS_EXAMPLE, ratings)
    p = tmp_path / "lumitv.toml"
    p.write_text(text, encoding="utf-8")
    return config.load(p, env_file=_env(tmp_path))


US_KIDS = """
[kids]
order = ["G", "PG", "PG-13", "R"]
ages = { G = 0, PG = 6, PG-13 = 12, R = 16 }
toddler_below_age = 6
toddler_allowed = ["G"]
no_age_max = "PG"
"""
US_RATINGS = """
[ratings]
country = "US"
fallback_country = "GB"

[ratings.fallback_map]
U = "G"
"12A" = "PG-13"
"""


def test_a_us_style_kids_order_works_end_to_end_with_its_own_ages(tmp_path):
    from lumitv.kids import allowed, cert_cutoff

    c = _with_sections(tmp_path, kids=US_KIDS, ratings=US_RATINGS)
    policy = c.kids_policy()
    assert policy.ages == {"G": 0, "PG": 6, "PG-13": 12, "R": 16} and policy.max_age == 15
    assert allowed("PG", 8, True, policy) and not allowed("PG-13", 8, True, policy)
    assert cert_cutoff(8, policy) == "PG" and cert_cutoff(99, policy) == "PG-13"  # no ValueError, no R
    assert not allowed("R", 99, True, policy)


def test_kids_label_without_an_age_is_rejected_by_name(tmp_path):
    with pytest.raises(ValueError, match=r"kids.*'PG-13'"):
        _with_sections(tmp_path, kids=US_KIDS.replace("PG-13 = 12, ", ""), ratings=US_RATINGS)
    with pytest.raises(ValueError, match=r"kids.*'TV-Y'"):
        _with_sections(tmp_path, kids=KIDS_EXAMPLE.replace('toddler_allowed = ["AL"]', 'toddler_allowed = ["AL", "TV-Y"]')
                       .replace('order = ["AL",', 'order = ["AL", "TV-Y",'))  # fmt: skip


def test_fallback_map_value_without_an_age_is_rejected_by_name(tmp_path):
    with pytest.raises(ValueError, match=r"fallback_map TV-MA = '21'"):
        _with_sections(tmp_path, ratings=RATINGS_EXAMPLE.replace('TV-MA = "18"', 'TV-MA = "21"'))


def test_max_age_defaults_to_15_and_is_configurable(tmp_path):
    c = _with_sections(tmp_path, kids=KIDS_EXAMPLE.replace("max_age = 15\n", ""))
    assert c.kids.max_age == 15 and c.kids.ages == {"AL": 0, "6": 6, "9": 9, "12": 12, "14": 14, "16": 16, "18": 18}
    c = _with_sections(tmp_path, kids=KIDS_EXAMPLE.replace("max_age = 15", "max_age = 11"))
    assert c.kids_policy().max_age == 11
    with pytest.raises(ValueError, match="max_age"):
        _with_sections(tmp_path, kids=KIDS_EXAMPLE.replace("max_age = 15", "max_age = -1"))


def _kids(default_from, default_until):
    return config.Kids(order=["AL", "6"], toddler_below_age=6, toddler_allowed=["AL"], no_age_max="6",
                       default_from=default_from, default_until=default_until)  # fmt: skip


@pytest.mark.kids_hours
@pytest.mark.parametrize(
    "hhmm, on", [("06:00", True), ("18:59", True), ("19:00", False), ("05:59", False), ("12:00", True)]
)
def test_kids_default_window_edges(hhmm, on):
    h, m = (int(x) for x in hhmm.split(":"))
    assert _kids("06:00", "19:00").default_on(datetime(2026, 11, 4, h, m)) is on


@pytest.mark.kids_hours
@pytest.mark.parametrize(
    "hhmm, on", [("23:00", True), ("03:00", True), ("12:00", False), ("20:00", True), ("08:00", False)]
)
def test_kids_default_overnight_window(hhmm, on):
    h, m = (int(x) for x in hhmm.split(":"))
    assert _kids("20:00", "08:00").default_on(datetime(2026, 11, 4, h, m)) is on


@pytest.mark.kids_hours
def test_kids_default_window_needs_both_ends():
    assert _kids(None, None).default_on(datetime(2026, 11, 4, 12, 0)) is False
    assert _kids("06:00", None).default_on(datetime(2026, 11, 4, 12, 0)) is False
