"""Family store: watch history, feedback, opened titles, taste profile.

Shares the SQLite file with tmdb.Cache but only touches its own tables.
"""

import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from lumitv.models import Kind, Viewer

Verdict = Literal["loved", "liked", "disliked"]

# The household tables, schema version by version: MIGRATIONS[i] takes the file from version i to i + 1 (SQLite's
# user_version). Append new steps; never edit a released one. Version 1 is the original schema (IF NOT EXISTS, so
# files from before versioning, at 0, pass through it unchanged).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS watched (title TEXT NOT NULL, date TEXT NOT NULL, source TEXT NOT NULL,
    raw_title TEXT NOT NULL, UNIQUE (raw_title, date, source));
CREATE TABLE IF NOT EXISTS feedback (tmdb_id INTEGER NULL, kind TEXT NULL, title TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('loved','liked','disliked')), note TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS opened (tmdb_id INTEGER, kind TEXT, title TEXT NOT NULL,
    service TEXT, at TEXT NOT NULL);
"""
MIGRATIONS: list[str] = [
    _SCHEMA,
    # 2: what happened when the TV opened a pick on a service (open_failed / play_check)
    """CREATE TABLE IF NOT EXISTS open_outcomes (tmdb_id INTEGER NOT NULL, kind TEXT NOT NULL, service TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN ('failed','played','no_play')), at TEXT NOT NULL);""",
    # 3: who watches this TV (set up on the TV), and who was watching when a title was opened
    """CREATE TABLE IF NOT EXISTS viewers (position INTEGER NOT NULL, name TEXT NOT NULL PRIMARY KEY, age INTEGER);
    ALTER TABLE opened ADD COLUMN viewers TEXT;""",
]
HOUSEHOLD_TABLES = ("watched", "feedback", "opened", "open_outcomes", "viewers")  # what a backup holds
OUTCOME_WINDOW_DAYS = 14  # older outcomes don't count: listings get fixed, apps get updated
FAILS_TO_DEMOTE = 2  # a service that failed to open this title this often goes last
NO_PLAYS_TO_DEMOTE = 3  # ... or that never played it in this many opens
BACKUPS_KEPT = 14  # daily household backups kept (the TMDB cache can be fetched again, so it is not backed up)
RECENT_OPENED = 10  # titles "recently opened" names


class NewerSchema(RuntimeError):
    """The file was written by a newer Lumi; refusing beats silently misreading it."""


def migrate(c: sqlite3.Connection) -> None:
    """Brings a connection's household tables to the newest schema, one recorded step at a time."""
    version = c.execute("PRAGMA user_version").fetchone()[0]
    if version > len(MIGRATIONS):
        raise NewerSchema(f"household tables are at schema {version}; this Lumi knows up to {len(MIGRATIONS)}")
    for i in range(version, len(MIGRATIONS)):
        c.executescript(MIGRATIONS[i])
        c.execute(f"PRAGMA user_version = {i + 1}")
        c.commit()


def _names(viewers: list[str] | None) -> str | None:
    return "\n".join(viewers) if viewers else None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class Store:
    """One connection for the server's life, in WAL mode with synchronous=NORMAL and a lock around every use (as
    tmdb.Cache): a connection opened and a transaction flushed per call put disk work on the event loop, and
    find_titles made one such call per card."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False, isolation_level=None)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            migrate(self._db)
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Several statements as one transaction (a single statement commits by itself)."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _write(self, sql: str, params: tuple) -> None:
        with self._lock:
            self._db.execute(sql, params)

    def _rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def add_watched(self, rows: list[tuple[str, str, str]], source: str) -> int:
        """Insert (show, date, raw_title) rows; duplicates are ignored. Returns rows added."""
        with self._tx() as c:
            before = c.total_changes
            c.executemany(
                "INSERT OR IGNORE INTO watched (title, date, source, raw_title) VALUES (?, ?, ?, ?)",
                [(t, d, source, raw) for t, d, raw in rows],
            )
            return c.total_changes - before

    def add_feedback(
        self, tmdb_id: int | None, kind: Kind | None, title: str, verdict: Verdict, note: str | None
    ) -> None:
        self._write(
            "INSERT INTO feedback (tmdb_id, kind, title, verdict, note, at) VALUES (?,?,?,?,?,?)",
            (tmdb_id, kind, title, verdict, note, _now()),
        )

    def add_opened(self, tmdb_id: int, kind: Kind, title: str, service: str, viewers: list[str] | None = None) -> None:
        """`viewers`: who was watching (names), so each person's taste can follow what they opened."""
        self._write(
            "INSERT INTO opened (tmdb_id, kind, title, service, at, viewers) VALUES (?,?,?,?,?,?)",
            (tmdb_id, kind, title, service, _now(), _names(viewers)),
        )

    def viewers(self) -> list[Viewer]:
        return [Viewer(name=n, age=a) for n, a in self._rows("SELECT name, age FROM viewers ORDER BY position")]

    def set_viewers(self, viewers: list[Viewer]) -> list[Viewer]:
        """Replaces who watches this TV (names unique ignoring case and surrounding spaces; the first spelling wins).
        Returns it."""
        seen: set[str] = set()
        kept = [v for v in viewers if not (v.name.casefold().strip() in seen or seen.add(v.name.casefold().strip()))]
        with self._tx() as c:
            c.execute("DELETE FROM viewers")
            c.executemany("INSERT INTO viewers (position, name, age) VALUES (?,?,?)",
                          [(i, v.name, v.age) for i, v in enumerate(kept)])  # fmt: skip
        return kept

    def services_used(self, tmdb_id: int, kind: Kind, title: str) -> set[str]:
        """Services the family opened this title on, or watched it on (imported history names its service)."""
        with self._lock:
            opened = self._db.execute(
                "SELECT service FROM opened WHERE tmdb_id=? AND kind=?", (tmdb_id, kind)
            ).fetchall()
            watched = self._db.execute(
                "SELECT DISTINCT source FROM watched WHERE lower(title)=lower(?)", (title,)
            ).fetchall()
        return {r[0] for r in opened + watched if r[0]}

    def add_outcome(
        self, tmdb_id: int, kind: Kind, service: str, outcome: Literal["failed", "played", "no_play"]
    ) -> None:
        self._write(
            "INSERT INTO open_outcomes (tmdb_id, kind, service, outcome, at) VALUES (?,?,?,?,?)",
            (tmdb_id, kind, service, outcome, _now()),
        )

    def bad_services_for(
        self, titles: Iterable[tuple[int, Kind]], days: int = OUTCOME_WINDOW_DAYS
    ) -> dict[tuple[int, Kind], set[str]]:
        """Per title, the services that look wrong for it lately: failed to open it FAILS_TO_DEMOTE times, or opened
        it NO_PLAYS_TO_DEMOTE times without anything playing (and never with). One query for all of them; titles
        with nothing against them are left out."""
        wanted = set(titles)
        if not wanted:
            return {}
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="microseconds")
        ids = sorted({t for t, _ in wanted})
        rows = self._rows(
            "SELECT tmdb_id, kind, service, outcome, COUNT(*) FROM open_outcomes "
            f"WHERE at >= ? AND tmdb_id IN ({','.join('?' * len(ids))}) GROUP BY tmdb_id, kind, service, outcome",
            (since, *ids),
        )
        n: dict[tuple[int, Kind], dict[tuple[str, str], int]] = {}
        for tmdb_id, kind, service, outcome, count in rows:
            if (tmdb_id, kind) in wanted:
                n.setdefault((tmdb_id, kind), {})[(service, outcome)] = count
        out: dict[tuple[int, Kind], set[str]] = {}
        for title, counts in n.items():
            bad = {
                s
                for s in {s for s, _ in counts}
                if counts.get((s, "failed"), 0) >= FAILS_TO_DEMOTE
                or (counts.get((s, "no_play"), 0) >= NO_PLAYS_TO_DEMOTE and not counts.get((s, "played"), 0))
            }
            if bad:
                out[title] = bad
        return out

    def bad_services(self, tmdb_id: int, kind: Kind, days: int = OUTCOME_WINDOW_DAYS) -> set[str]:
        """bad_services_for one title."""
        return self.bad_services_for([(tmdb_id, kind)], days).get((tmdb_id, kind), set())

    def seen_titles(self) -> set[str]:
        return {r[0].lower() for r in self._rows("SELECT title FROM watched UNION SELECT title FROM opened")}

    def taste_profile(self, top_n: int = 30, viewers: list[str] | None = None) -> dict:
        """With `viewers`, "recently opened" counts only titles opened while one of them was watching (or before
        anyone was recorded), so a child's picks don't steer the grown-ups' evening and the other way round. A title
        opened several times is named once, at its newest place."""
        with self._lock:
            most = self._db.execute(
                "SELECT title, COUNT(*) AS n FROM watched GROUP BY title ORDER BY n DESC, title LIMIT ?", (top_n,)
            ).fetchall()
            verdicts = self._db.execute("SELECT DISTINCT title, verdict FROM feedback ORDER BY title").fetchall()
            opened = self._db.execute(
                "SELECT title, viewers FROM opened ORDER BY at DESC, rowid DESC LIMIT 200"
            ).fetchall()
        recent = dict.fromkeys(
            title for title, names in opened if not viewers or not names or set(names.split("\n")) & set(viewers)
        )
        return {
            "most_watched": [{"title": t, "count": n} for t, n in most],
            "loved": [t for t, v in verdicts if v == "loved"],
            "disliked": [t for t, v in verdicts if v == "disliked"],
            "recently_opened": list(recent)[:RECENT_OPENED],
        }


def backup(db_path: str | Path, dest_dir: str | Path, day: date | None = None, keep: int = BACKUPS_KEPT) -> Path:
    """Copies the household tables into dest_dir/household-<day>.db (written to a temp file, then renamed, so a
    crash never leaves half a backup) and keeps the newest `keep`. Returns the backup's path."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / f"household-{(day or date.today()).isoformat()}.db"
    tmp = final.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    with closing(sqlite3.connect(tmp)) as out:
        migrate(out)
        out.execute("ATTACH DATABASE ? AS src", (str(db_path),))
        with out:
            for t in HOUSEHOLD_TABLES:
                out.execute(f"INSERT INTO main.{t} SELECT * FROM src.{t}")
        out.execute("DETACH DATABASE src")
    os.replace(tmp, final)
    for old in sorted(dest_dir.glob("household-*.db"))[:-keep]:
        old.unlink()
    return final


def restore(db_path: str | Path, backup_path: str | Path) -> dict[str, int]:
    """Replaces the household tables in db_path with a backup's (the cache is untouched). Returns rows per table."""
    with closing(sqlite3.connect(db_path, timeout=5)) as c:
        migrate(c)
        c.execute("ATTACH DATABASE ? AS bak", (str(backup_path),))
        counts = {}
        with c:
            for t in HOUSEHOLD_TABLES:
                c.execute(f"DELETE FROM main.{t}")
                c.execute(f"INSERT INTO main.{t} SELECT * FROM bak.{t}")
                counts[t] = c.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
        c.execute("DETACH DATABASE bak")
    return counts
