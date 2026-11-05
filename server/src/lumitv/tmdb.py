import asyncio
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx

from .catalog_source import CatalogSource, Details, TmdbUnavailable
from .config import Ratings
from .kids import DEFAULT_AGES
from .models import Kind

log = logging.getLogger("lumitv.tmdb")

BASE_URL = "https://api.themoviedb.org/3"
POSTER_BASE = "https://image.tmdb.org/t/p/w342"
TTL_S = 24 * 3600
KEYWORD_TTL_S = 30 * 24 * 3600  # keyword ids and how many titles carry a keyword barely change
CACHE_MAX_AGE_S = 45 * 24 * 3600  # past every TTL: nothing older is ever served fresh, only as an outage fallback
CACHE_MAX_ROWS = 300_000  # a few hundred MB at most
CACHE_MEMORY_KB = 256 * 1024  # SQLite page cache for the HTTP cache connection (pages are only used as read)
PRUNE_BATCH = 5000  # rows per DELETE when pruning: the lock is released between batches
DEFAULT_CONCURRENCY = 16  # TMDB requests in flight at once, all callers together ([server] lookup_concurrency)
RETRY_AFTER_MAX_S = 5.0  # a 429's Retry-After is honoured up to this, then the request is tried once more
RETRY_AFTER_DEFAULT_S = 1.0  # ... and this long when the header is missing or unreadable


class Cache:
    """HTTP response cache in SQLite (table http_cache), shared with the Store's DB file.

    One connection for the server's life, in WAL mode with synchronous=NORMAL: a read never waits for a write, and a
    write doesn't flush the disk. With a new connection and a committed, flushed transaction per call, the warm job's
    hundreds of writes on a busy shared PC blocked the event loop for up to 19.5 s (loop_stall, 2026-11-04), stalling
    the TV link, uploads and Claude's tool calls together."""

    def __init__(self, db_path: Path | str, ttl_s: float = TTL_S):
        self.db_path = str(db_path)
        self.ttl_s = ttl_s
        self._lock = threading.Lock()  # one connection; the speech/thread pool code never touches it, but be safe
        self._db = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")  # stored in the file: the Store's connections use it too
        self._db.execute("PRAGMA synchronous=NORMAL")
        # keep the whole cache (~130 MB on the reference PC) in memory: with SQLite's 2 MB default nearly every read
        # went to a disk another workload kept 225% busy, and one read could take seconds (2026-11-04)
        self._db.execute(f"PRAGMA cache_size=-{CACHE_MEMORY_KB}")
        self._db.execute("CREATE TABLE IF NOT EXISTS http_cache(url TEXT PRIMARY KEY, body TEXT, fetched_at REAL)")

    def get(self, url: str) -> tuple[str, float] | None:
        with self._lock:
            row = self._db.execute("SELECT body, fetched_at FROM http_cache WHERE url=?", (url,)).fetchone()
        return (row[0], row[1]) if row else None

    def put(self, url: str, body: str, fetched_at: float | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO http_cache(url, body, fetched_at) VALUES (?,?,?)",
                (url, body, time.time() if fetched_at is None else fetched_at),
            )

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def prune(
        self, max_age_s: float = CACHE_MAX_AGE_S, max_rows: int = CACHE_MAX_ROWS, batch: int = PRUNE_BATCH
    ) -> int:
        """Drops entries older than max_age_s, then the oldest beyond max_rows, `batch` rows per statement with the
        lock released in between: the daily job runs this in a thread while the event loop keeps reading the cache.
        Returns how many went."""
        gone = 0
        too_old = time.time() - max_age_s
        while True:
            with self._lock:
                n = self._db.execute(
                    "DELETE FROM http_cache WHERE rowid IN (SELECT rowid FROM http_cache WHERE fetched_at < ? LIMIT ?)",
                    (too_old, batch),
                ).rowcount
            gone += n
            if n < batch:
                break
        while True:
            with self._lock:
                excess = self._db.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] - max_rows
                if excess <= 0:
                    break
                n = self._db.execute(
                    "DELETE FROM http_cache WHERE rowid IN (SELECT rowid FROM http_cache ORDER BY fetched_at LIMIT ?)",
                    (min(excess, batch),),
                ).rowcount
            gone += n
        return gone

    def is_fresh(self, fetched_at: float, ttl_s: float | None = None) -> bool:
        return time.time() - fetched_at < (self.ttl_s if ttl_s is None else ttl_s)


def _retry_wait(resp: httpx.Response) -> float:
    """Seconds to wait after a 429: its Retry-After (seconds), capped; the default when absent or not a number."""
    try:
        wait = float(resp.headers.get("retry-after", ""))
    except ValueError:
        return RETRY_AFTER_DEFAULT_S
    return min(max(wait, 0.0), RETRY_AFTER_MAX_S)


class Tmdb(CatalogSource):
    def __init__(
        self,
        token: str,
        cache: Cache,
        ratings: Ratings,
        timeout_s: float,
        on_cache: Callable[[bool], None] | None = None,
        on_stale: Callable[[str, str], None] | None = None,
        ages: Mapping[str, int] | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
    ):
        """`ages`: [kids] ages (certificate label -> minimum age), which ranks certificates by strictness; None: the
        default table (a config with a table of its own must pass it). `concurrency` bounds the requests in flight
        across every caller (a cold "like X" alone would fire 60-100 at once; TMDB allows ~40 a second)."""
        self._cache = cache
        self._ratings = ratings
        self._ages = dict(DEFAULT_AGES if ages is None else ages)
        self._timeout_s = timeout_s
        self._on_cache = on_cache
        self._on_stale = on_stale
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self.rate_limited = 0  # 429s seen (each waited Retry-After and was tried once more)
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout_s,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- transport -------------------------------------------------------------------------

    async def _get(self, path: str, params: dict | None = None, ttl_s: float | None = None) -> dict:
        """`ttl_s`: how long a cached answer stays fresh (default: the cache's)."""
        req = self._client.build_request("GET", path, params=params)
        key = str(req.url)
        cached = self._cache.get(key)
        if cached and self._cache.is_fresh(cached[1], ttl_s):
            self._note(True)
            return json.loads(cached[0])
        self._note(False)
        try:
            resp = await self._send(req)
            body = resp.text
            data = json.loads(body)
        except (httpx.HTTPError, TimeoutError, ValueError) as e:
            if cached:  # stale beats nothing, but say so: a bad token would otherwise hide here
                if self._on_stale:
                    self._on_stale(path, repr(e))
                return json.loads(cached[0])
            raise TmdbUnavailable(f"TMDB request failed for {path}: {e!r}") from e
        self._cache.put(key, body)
        return data

    async def _send(self, req: httpx.Request) -> httpx.Response:
        """One request within the concurrency bound; on a 429 wait Retry-After (RETRY_AFTER_MAX_S at most) and try
        once more. Anything but a 2xx raises httpx.HTTPStatusError."""
        resp = await self._attempt(req)
        if resp.status_code == 429:
            self.rate_limited += 1
            wait = _retry_wait(resp)
            log.warning("TMDB rate limit on %s: waiting %.1f s", req.url.path, wait)
            await asyncio.sleep(wait)
            resp = await self._attempt(req)
            if resp.status_code == 429:
                self.rate_limited += 1
        resp.raise_for_status()
        return resp

    async def _attempt(self, req: httpx.Request) -> httpx.Response:
        async with self._sem:
            async with asyncio.timeout(self._timeout_s):
                return await self._client.send(req)

    def _note(self, hit: bool) -> None:
        if self._on_cache:
            self._on_cache(hit)

    # ---- catalog ---------------------------------------------------------------------------

    @staticmethod
    def _rows(results: list[dict]) -> list[dict]:
        """TMDB's adult titles are never candidates, whichever list they come from (similar/recommendations have no
        include_adult switch)."""
        return [r for r in results if not r.get("adult")]

    async def search(self, query: str, kind: Kind | None) -> list[dict]:
        params = {"query": query, "include_adult": "false"}  # TMDB's default, stated: a family product can't assume it
        if kind is None:
            data = await self._get("/search/multi", params)
            return [r for r in self._rows(data.get("results", [])) if r.get("media_type") in ("movie", "tv")]
        data = await self._get(f"/search/{kind}", params)
        return [{**r, "media_type": kind} for r in self._rows(data.get("results", []))]

    async def similar(self, tmdb_id: int, kind: Kind) -> list[dict]:
        recs, sim = await asyncio.gather(
            self._get(f"/{kind}/{tmdb_id}/recommendations"),
            self._get(f"/{kind}/{tmdb_id}/similar"),
        )
        out: list[dict] = []
        seen: set[int] = set()
        for r in self._rows(recs.get("results", []) + sim.get("results", [])):
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append({**r, "media_type": kind})
        return out

    async def discover(
        self,
        kind: Kind,
        region: str,
        provider_ids: set[int],
        genres: list[int] | None,
        certification_lte: str | None,
        page: int = 1,
        any_genre: bool = False,
        min_votes: int | None = None,
        keywords: list[int] | None = None,
    ) -> list[dict]:
        params: dict = {
            "with_watch_providers": "|".join(str(p) for p in sorted(provider_ids)),
            "watch_region": region,
            "with_watch_monetization_types": "flatrate",
            "include_adult": "false",
        }
        if genres:
            params["with_genres"] = ("|" if any_genre else ",").join(str(g) for g in genres)
        if min_votes:
            params["vote_count.gte"] = min_votes
        if keywords:
            params["with_keywords"] = "|".join(str(k) for k in keywords)
        if kind == "movie" and certification_lte:
            params["certification_country"] = self._ratings.country
            params["certification.lte"] = certification_lte
        if page > 1:  # page 1 keeps the plain URL so find_titles and warming share cache entries
            params["page"] = page
        data = await self._get(f"/discover/{kind}", params)
        return [{**r, "media_type": kind} for r in self._rows(data.get("results", []))]

    async def external_ids(self, kind: Kind, tmdb_id: int) -> dict:
        """Other databases' ids for a title (wikidata_id, freebase_mid, imdb_id, ...); values may be null."""
        return await self._get(f"/{kind}/{tmdb_id}/external_ids")

    async def keyword_ids(self, names: list[str]) -> dict[str, int]:
        async def one(name: str) -> int | None:
            data = await self._get("/search/keyword", {"query": name}, ttl_s=KEYWORD_TTL_S)
            return next((r["id"] for r in data.get("results", []) if r.get("name", "").lower() == name.lower()), None)

        found = await asyncio.gather(*(one(n) for n in names))
        return {n: i for n, i in zip(names, found, strict=True) if i is not None}

    async def keyword_count(self, keyword_id: int) -> int:
        movies, series = await asyncio.gather(
            self._get("/discover/movie", {"with_keywords": str(keyword_id)}, ttl_s=KEYWORD_TTL_S),
            self._get("/discover/tv", {"with_keywords": str(keyword_id)}, ttl_s=KEYWORD_TTL_S),
        )
        return int(movies.get("total_results") or 0) + int(series.get("total_results") or 0)

    async def genres(self, kind: Kind) -> dict[str, int]:
        data = await self._get(f"/genre/{kind}/list", {"language": "en"})
        return {g["name"]: g["id"] for g in data.get("genres", [])}

    async def details(self, tmdb_id: int, kind: Kind, exact_minutes: bool = False) -> Details:
        extra = "release_dates" if kind == "movie" else "content_ratings"
        d = await self._get(f"/{kind}/{tmdb_id}", {"append_to_response": f"watch/providers,{extra},keywords"})
        date = d.get("release_date") if kind == "movie" else d.get("first_air_date")
        poster = d.get("poster_path")
        flatrate = {
            region: {p["provider_id"] for p in info.get("flatrate", [])}
            for region, info in (d.get("watch/providers") or {}).get("results", {}).items()
            if info.get("flatrate")
        }
        runtime = seasons = season1 = season2 = total = None
        if kind == "movie":
            runtime = d.get("runtime") or None
            cert = self._movie_certs(d)
        else:
            seasons = d.get("number_of_seasons")
            cert = self._tv_certs(d)
            season1, season2, total = await self._tv_minutes(tmdb_id, d, exact_minutes)
        return Details(
            title=d.get("title") or d.get("name") or "",
            year=int(date[:4]) if date else None,
            overview=d.get("overview") or "",
            poster_url=f"{POSTER_BASE}{poster}" if poster else None,
            certification=self._resolve_cert(cert),
            adult=bool(d.get("adult")),
            runtime_minutes=runtime,
            seasons=seasons,
            season1_minutes=season1,
            season2_minutes=season2,
            total_minutes=total,
            flatrate_by_region=flatrate,
            genre_ids=[g["id"] for g in d.get("genres") or [] if "id" in g],
            # films list them under "keywords", series under "results"
            keywords={k["id"]: k.get("name", "") for k in (kw.get("keywords") or kw.get("results") or []) if "id" in k}
            if isinstance(kw := d.get("keywords"), dict)
            else {},
        )

    # ---- helpers ---------------------------------------------------------------------------

    async def _tv_minutes(self, tmdb_id: int, d: dict, exact: bool) -> tuple[int | None, int | None, int | None]:
        """(season 1 minutes, season 2 minutes, total minutes). Estimated from the main response when it
        carries usable episode runtimes and exact minutes aren't needed; otherwise seasons 1 and 2 are
        fetched (concurrently) and summed."""
        n_episodes = d.get("number_of_episodes")
        typical = [m for m in d.get("episode_run_time") or [] if m]
        if typical and not exact:
            avg = sum(typical) / len(typical)

            def season_eps(n: int) -> int | None:
                return next(
                    (x.get("episode_count") for x in d.get("seasons") or [] if x.get("season_number") == n), None
                )

            s1_eps, s2_eps = season_eps(1), season_eps(2)
            return (
                round(avg * s1_eps) if s1_eps else None,
                round(avg * s2_eps) if s2_eps else None,
                round(avg * n_episodes) if n_episodes else None,
            )
        want_two = (d.get("number_of_seasons") or 0) >= 2
        s1, s2 = await asyncio.gather(
            self._season_runtimes(tmdb_id, 1), self._season_runtimes(tmdb_id, 2) if want_two else asyncio.sleep(0, [])
        )
        if not s1:
            return None, None, None
        season1 = sum(s1)
        total = round(season1 / len(s1) * n_episodes) if n_episodes else None
        return season1, (sum(s2) if s2 else None), total

    async def _season_runtimes(self, tmdb_id: int, season: int) -> list[int]:
        try:
            data = await self._get(f"/tv/{tmdb_id}/season/{season}")
        except TmdbUnavailable:
            return []
        return [e["runtime"] for e in data.get("episodes", []) if e.get("runtime")]

    @staticmethod
    def _movie_certs(d: dict) -> dict[str, list[str]]:
        """Country -> every certification among its release entries (cinema, DVD, TV, ... can differ)."""
        out: dict[str, list[str]] = {}
        for entry in (d.get("release_dates") or {}).get("results", []):
            certs = [rd["certification"] for rd in entry.get("release_dates", []) if rd.get("certification")]
            if certs:
                out.setdefault(entry["iso_3166_1"], []).extend(certs)
        return out

    @staticmethod
    def _tv_certs(d: dict) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for e in (d.get("content_ratings") or {}).get("results", []):
            if e.get("rating"):
                out.setdefault(e["iso_3166_1"], []).append(e["rating"])
        return out

    def _resolve_cert(self, certs: dict[str, list[str]]) -> str | None:
        """The strictest of the home country's certificates by age (AL in cinemas but 12 on DVD is a 12),
        cross-checked with the fallback country's mapped ones: the stricter wins (NL AL but US R is a 16). Labels
        without an age never count; one is returned only when nothing with an age is there (kids mode excludes it)."""
        home = certs.get(self._ratings.country, [])
        mapped = [self._ratings.fallback_map.get(c) for c in certs.get(self._ratings.fallback_country, [])]
        known = [c for c in (*home, *mapped) if c in self._ages]
        if known:
            return max(known, key=self._ages.__getitem__)  # ties keep the home country's label
        return home[0] if home else None
