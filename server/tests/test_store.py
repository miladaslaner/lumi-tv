import sqlite3
from pathlib import Path

import pytest

from lumitv import netflix_import
from lumitv.store import Store
from lumitv.tmdb import Cache

FIXTURE = Path(__file__).parent / "fixtures" / "NetflixViewingHistory.csv"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "lumitv.db")


def test_parse_strips_episode_suffixes():
    rows = dict(netflix_import.parse(FIXTURE))
    assert "Suits" in rows and "The Matrix" in rows
    assert "Adolescence" in rows
    assert "Fast, Furious and Funny" in rows


def test_parse_title_rules(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text(
        'Title,Date\n"Suits: Season 1: Pilot","1/1/26"\n"A: Part 2: B","1/2/26"\n'
        '"C: Volume 1: D","1/3/26"\n"E: Chapter 4","1/4/26"\n"Mission: Impossible","1/5/26"\n'
    )
    assert netflix_import.parse(p) == [
        ("Suits", "1/1/26"),
        ("A", "1/2/26"),
        ("C", "1/3/26"),
        ("E", "1/4/26"),
        ("Mission: Impossible", "1/5/26"),
    ]


def test_import_is_idempotent(store):
    first = netflix_import.import_csv(store, FIXTURE)
    assert first == 6
    assert netflix_import.import_csv(store, FIXTURE) == 0
    with sqlite3.connect(store.db_path) as c:
        assert c.execute("select count(*) from watched").fetchone()[0] == 6


def test_taste_profile_most_watched(store):
    netflix_import.import_csv(store, FIXTURE)
    mw = store.taste_profile()["most_watched"]
    assert mw[0] == {"title": "Suits", "count": 3}
    assert [m["title"] for m in mw[1:]] == sorted(m["title"] for m in mw[1:])


def test_feedback_in_loved_and_disliked(store):
    store.add_feedback(1, "tv", "Suits", "loved", None)
    store.add_feedback(2, "movie", "Cats", "disliked", "too weird")
    store.add_feedback(None, None, "Meh", "liked", None)
    p = store.taste_profile()
    assert p["loved"] == ["Suits"]
    assert p["disliked"] == ["Cats"]


def test_opened_recent_and_seen_titles(store):
    netflix_import.import_csv(store, FIXTURE)
    for i in range(12):
        store.add_opened(i, "movie", f"Film {i}", "netflix")
    recent = store.taste_profile()["recently_opened"]
    assert len(recent) == 10 and recent[0] == "Film 11"
    seen = store.seen_titles()
    assert "suits" in seen and "film 0" in seen and "the matrix" in seen


def test_coexists_with_cache_and_top_n(tmp_path):
    db = tmp_path / "lumitv.db"
    Cache(db)
    s = Store(db)
    s.add_opened(1, "tv", "X", "netflix")
    assert s.add_watched([("A", "1/1/26", "A: Pilot"), ("A", "1/2/26", "A: Two"), ("B", "1/3/26", "B")], "netflix") == 3
    Cache(db).put("https://api.example/x", "{}")
    assert s.taste_profile(top_n=1)["most_watched"] == [{"title": "A", "count": 2}]
    assert len(s.taste_profile()["most_watched"]) == 2


def test_same_day_episodes_count_separately_and_reimport_is_idempotent(store, tmp_path):
    p = tmp_path / "binge.csv"
    p.write_text(
        'Title,Date\n"Suits: Season 1: Pilot","1/1/26"\n"Suits: Season 1: Bail Out","1/1/26"\n'
        '"Suits: Season 1: Pilot","1/1/26"\n'
    )
    assert netflix_import.import_csv(store, p) == 2
    assert netflix_import.import_csv(store, p) == 0
    assert store.taste_profile()["most_watched"] == [{"title": "Suits", "count": 2}]
