import sqlite3
import time
from datetime import date

import pytest

from lumitv import store as store_mod
from lumitv.store import NewerSchema, Store, backup, restore
from lumitv.tmdb import Cache


def test_new_and_old_files_reach_the_newest_schema(tmp_path):
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as c:  # a file from before versioning: tables there, user_version 0
        c.executescript(store_mod._SCHEMA)
    Store(old)
    with sqlite3.connect(old) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == len(store_mod.MIGRATIONS)


def test_a_file_from_a_newer_lumi_is_refused(tmp_path):
    db = tmp_path / "new.db"
    with sqlite3.connect(db) as c:
        c.execute(f"PRAGMA user_version = {len(store_mod.MIGRATIONS) + 1}")
    with pytest.raises(NewerSchema):
        Store(db)


def test_backup_holds_household_tables_only_and_keeps_the_newest(tmp_path):
    db = tmp_path / "lumitv.db"
    s = Store(db)
    s.add_opened(1, "tv", "Suits", "netflix")
    s.add_feedback(1, "tv", "Suits", "loved", None)
    Cache(db).put("https://api.example/x", "{}")
    for d in range(1, 17):
        path = backup(db, tmp_path / "backups", day=date(2026, 11, d), keep=14)
    assert sorted(p.name for p in (tmp_path / "backups").iterdir())[0] == "household-2026-11-03.db"
    assert len(list((tmp_path / "backups").glob("household-*.db"))) == 14
    with sqlite3.connect(path) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == set(store_mod.HOUSEHOLD_TABLES)  # no cache
        assert c.execute("SELECT service FROM opened").fetchone()[0] == "netflix"


def test_restore_brings_the_household_back_and_leaves_the_cache(tmp_path):
    db = tmp_path / "lumitv.db"
    s = Store(db)
    s.add_opened(1, "tv", "Suits", "netflix")
    cache = Cache(db)
    cache.put("https://api.example/x", "{}")
    saved = backup(db, tmp_path / "b")
    s.add_opened(2, "movie", "Heat", "prime")
    assert restore(db, saved)["opened"] == 1
    assert [t for (t,) in sqlite3.connect(db).execute("SELECT title FROM opened")] == ["Suits"]
    assert cache.get("https://api.example/x") is not None


def test_cache_prune_drops_old_then_oldest(tmp_path):
    cache = Cache(tmp_path / "c.db")
    now = time.time()
    cache.put("old", "{}", fetched_at=now - 50 * 86400)
    for i in range(5):
        cache.put(f"u{i}", "{}", fetched_at=now - i)
    assert cache.prune(max_age_s=45 * 86400, max_rows=3) == 3
    assert [cache.get(u) is not None for u in ("u0", "u1", "u2", "u3", "u4", "old")] == [
        True,
        True,
        True,
        False,
        False,
        False,
    ]


def test_bad_services_from_failures_and_plays(tmp_path):
    s = Store(tmp_path / "l.db")
    s.add_outcome(1, "tv", "netflix", "failed")
    assert s.bad_services(1, "tv") == set()  # one failure is noise
    s.add_outcome(1, "tv", "netflix", "failed")
    for _ in range(3):
        s.add_outcome(1, "tv", "prime", "no_play")
    s.add_outcome(1, "tv", "videoland", "no_play")
    assert s.bad_services(1, "tv") == {"netflix", "prime"}
    s.add_outcome(1, "tv", "prime", "played")  # it did play once: the listing is right
    assert s.bad_services(1, "tv") == {"netflix"}
    assert s.bad_services(1, "movie") == set() and s.bad_services(2, "tv") == set()


def test_bad_services_for_many_titles_is_one_query_and_is_kind_qualified(tmp_path):
    s = Store(tmp_path / "l.db")
    for _ in range(2):
        s.add_outcome(1, "tv", "netflix", "failed")
        s.add_outcome(7, "movie", "prime", "failed")
    s.add_outcome(1, "movie", "videoland", "failed")  # the film with the same id: one failure, noise
    statements = []
    s._db.set_trace_callback(statements.append)
    bad = s.bad_services_for([(1, "tv"), (1, "movie"), (7, "movie"), (9, "tv")])
    assert bad == {(1, "tv"): {"netflix"}, (7, "movie"): {"prime"}}  # clean titles are left out
    assert len([x for x in statements if x.startswith("SELECT")]) == 1
    assert s.bad_services_for([]) == {}


def test_cache_prune_works_in_batches_and_releases_the_lock_between_them(tmp_path):
    cache = Cache(tmp_path / "c.db")
    now = time.time()
    for i in range(12):
        cache.put(f"old{i}", "{}", fetched_at=now - 50 * 86400)
    for i in range(7):
        cache.put(f"u{i}", "{}", fetched_at=now - i)

    class CountingLock:
        def __init__(self, inner):
            self.inner, self.entered = inner, 0

        def __enter__(self):
            self.entered += 1
            return self.inner.__enter__()

        def __exit__(self, *exc):
            return self.inner.__exit__(*exc)

    lock = cache._lock = CountingLock(cache._lock)
    assert cache.prune(max_age_s=45 * 86400, max_rows=3, batch=5) == 12 + 4
    # 12 old rows in batches of 5 (5, 5, 2), then the 4 over the cap in one, then the count that finds nothing over
    assert lock.entered == 5
    assert [cache.get(u) is not None for u in ("u0", "u1", "u2", "u3", "old0")] == [True, True, True, False, False]
    assert cache.prune(max_age_s=45 * 86400, max_rows=3, batch=5) == 0


def test_old_outcomes_stop_counting(tmp_path):
    s = Store(tmp_path / "l.db")
    for _ in range(2):
        s.add_outcome(1, "tv", "netflix", "failed")
    with sqlite3.connect(tmp_path / "l.db") as c:
        c.execute("UPDATE open_outcomes SET at = '2020-01-01T00:00:00+00:00'")
    assert s.bad_services(1, "tv") == set()


def test_viewers_are_kept_in_order_with_unique_names(tmp_path):
    from lumitv.models import Viewer

    s = Store(tmp_path / "l.db")
    kept = s.set_viewers(
        [Viewer(name="Alex"), Viewer(name="Emma", age=6), Viewer(name="emma", age=9), Viewer(name=" EMMA ")]
    )
    assert [v.label() for v in kept] == ["Alex", "Emma (6)"]  # case and surrounding spaces don't make a new person
    assert [v.label() for v in s.viewers()] == ["Alex", "Emma (6)"]
    assert s.set_viewers([]) == [] and s.viewers() == []


def test_taste_follows_who_was_watching(tmp_path):
    s = Store(tmp_path / "l.db")
    s.add_opened(1, "tv", "Bluey", "netflix", ["Emma"])
    s.add_opened(2, "tv", "Suits", "netflix", ["Alex"])
    s.add_opened(3, "movie", "Heat", "prime")  # from before anyone was recorded: counts for all
    assert s.taste_profile(viewers=["Alex"])["recently_opened"] == ["Heat", "Suits"]
    assert s.taste_profile()["recently_opened"] == ["Heat", "Suits", "Bluey"]


def test_recently_opened_names_a_title_once_at_its_newest_place(tmp_path):
    s = Store(tmp_path / "l.db")
    s.add_opened(1, "tv", "Suits", "netflix")
    s.add_opened(2, "movie", "Heat", "prime")
    s.add_opened(1, "tv", "Suits", "prime")
    assert s.taste_profile()["recently_opened"] == ["Suits", "Heat"]
    for i in range(12):
        s.add_opened(10 + i, "movie", f"Film {i}", "netflix")
    assert len(s.taste_profile()["recently_opened"]) == 10


def test_store_shares_the_file_with_other_connections_and_closes(tmp_path):
    db = tmp_path / "l.db"
    s = Store(db)
    s.add_opened(1, "tv", "Suits", "netflix")
    with sqlite3.connect(db) as other:  # the backup and restore helpers open their own connections
        assert other.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        other.execute(
            "INSERT INTO opened (tmdb_id, kind, title, service, at) VALUES (2, 'movie', 'Heat', 'prime', 'z')"
        )
    assert s.taste_profile()["recently_opened"] == ["Heat", "Suits"]  # the long-lived connection sees it
    s.close()
    with pytest.raises(sqlite3.ProgrammingError):
        s.viewers()
