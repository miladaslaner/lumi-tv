"""Cache warming: discover the popular titles per service and fetch their details ahead of time."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .catalog import HOURS_DISCOVER_PAGES, genre_ids
from .catalog_source import CatalogSource
from .kids import KidsPolicy, cert_cutoff
from .metrics import Metrics
from .models import Kind
from .services import Service
from .title_ids import TitleIdResolver

log = logging.getLogger("lumitv.warm")
PAGES = 5


@dataclass
class WarmResult:
    attempted: int = 0  # distinct titles whose details were requested
    warmed: int = 0
    discover_failed: int = 0
    exact_failed: int = 0  # series whose exact season lengths could not be fetched
    title_ids: int = 0  # titles with at least one id their service wants (Google TV, Netflix)

    @property
    def failed(self) -> int:
        return self.attempted - self.warmed


async def warm_detailed(
    tmdb: CatalogSource,
    services: dict[str, Service],
    concurrency: int = 8,
    genres: list[str] | None = None,
    genre_aliases: dict[str, list[str]] | None = None,
    title_ids: TitleIdResolver | None = None,
    min_votes: int | None = None,
    genre_keywords: dict[str, list[str]] | None = None,
    kids_policy: KidsPolicy | None = None,
    kids_min_votes: int | None = None,
) -> WarmResult:
    """Page 1..PAGES of TMDB's default (popularity) discover order for every service x kind in the
    service's own region (flatrate), plus page 1 for each of `genres` (resolved with the same alias mapping
    find_titles uses; pages 1..HOURS_DISCOVER_PAGES, which the hours path reads), then details for each
    distinct title and, for series, the exact season lengths hours questions look up.

    With `kids_policy`, the kids-mode variants of those discovers are warmed too (kids mode is the daytime
    default): the certificate cutoffs of a kids ask without an age and of a toddler, with `kids_min_votes` as the
    vote floor, pages 1..HOURS_DISCOVER_PAGES. Other ages' cutoffs ("9", "12", "14") stay cold, and so do the
    keyword and genre-bridged discovers of "like X" (they depend on the liked title).

    `min_votes`, `kids_min_votes` and `genre_keywords` must be what find_titles passes (ranking.min_votes and
    ranking.kids_min_votes; the series keyword filter), or the warmed URLs are not the ones asks read.

    With `title_ids`, titles found for services that want per-title ids (Google TV page, Netflix link) get them
    resolved (and cached), so show_on_tv rarely waits for one."""
    result = WarmResult()
    sem = asyncio.Semaphore(max(1, concurrency))
    kinds: tuple[Kind, ...] = ("movie", "tv")
    # (certificate cutoff, vote floor, pages) of every discover variant find_titles reads: grown-ups first
    variants: list[tuple[str | None, int | None, int]] = [(None, min_votes, PAGES)]
    if kids_policy:
        cutoffs = dict.fromkeys(
            [cert_cutoff(None, kids_policy), cert_cutoff(kids_policy.toddler_below_age - 1, kids_policy)]
        )
        variants += [(cutoff, kids_min_votes, HOURS_DISCOVER_PAGES) for cutoff in cutoffs]

    async def genre_list(kind: Kind) -> None:  # fills the cache find_titles reads genre names from
        try:
            await tmdb.genres(kind)
        except Exception as e:
            log.warning("warm: genre list %s failed: %r", kind, e)

    await asyncio.gather(*(genre_list(k) for k in kinds))

    async def pages(
        s: Service,
        kind: Kind,
        ids: list[int] | None,
        keywords: list[int] | None,
        cutoff: str | None,
        votes: int | None,
        n: int,
    ) -> list[tuple[int, Kind]]:
        out: list[tuple[int, Kind]] = []
        extra = {"keywords": keywords} if keywords else {}
        for page in range(1, n + 1):
            try:
                async with sem:
                    rows = await tmdb.discover(
                        kind, s.region, set(s.tmdb_provider_ids), ids, cutoff, page=page, min_votes=votes, **extra
                    )
            except Exception as e:
                log.warning("warm: discover %s/%s genres %s page %d failed: %r", s.id, kind, ids, page, e)
                result.discover_failed += 1
                break
            if not rows:
                break
            out += [(r["id"], kind) for r in rows if "id" in r]
        return out

    active = [s for s in services.values() if s.tmdb_provider_ids and s.region]
    jobs = [
        (s, pages(s, k, None, None, cutoff, votes, n)) for cutoff, votes, n in variants for s in active for k in kinds
    ]
    for name in genres or []:
        per_kind = await genre_ids(tmdb, [name], genre_aliases or {}, list(kinds), None)
        series_kw: list[int] = []
        if (genre_keywords or {}).get(name.lower()):
            try:
                series_kw = list((await tmdb.keyword_ids(genre_keywords[name.lower()])).values())  # type: ignore[index]
            except Exception as e:
                log.warning("warm: keyword ids for %s failed: %r", name, e)
        jobs += [
            (s, pages(s, k, ids, series_kw if k == "tv" else None, cutoff, votes, HOURS_DISCOVER_PAGES))
            for cutoff, votes, _ in variants
            for k, ids in per_kind.items()
            for s in active
        ]
    found_per_job = await asyncio.gather(*(job for _, job in jobs))
    titles = list(dict.fromkeys(t for found in found_per_job for t in found))
    wanted: dict[tuple[int, Kind], frozenset[str]] = {}
    for (s, _), found in zip(jobs, found_per_job, strict=True):
        for t in found:
            wanted[t] = wanted.get(t, frozenset()) | s.wanted_ids

    async def resolve_ids(t: tuple[int, Kind]) -> None:
        async with sem:
            if await title_ids.resolve(t[1], t[0], wanted[t]):  # type: ignore[union-attr]
                result.title_ids += 1

    async def fetch(t: tuple[int, Kind]) -> bool:
        try:
            async with sem:
                await tmdb.details(*t)
        except Exception:
            return False
        if t[1] == "tv":
            try:
                async with sem:
                    await tmdb.details(*t, exact_minutes=True)
            except Exception:
                result.exact_failed += 1
        return True

    result.attempted = len(titles)
    result.warmed = sum(await asyncio.gather(*(fetch(t) for t in titles)))
    if title_ids:
        await asyncio.gather(*(resolve_ids(t) for t in titles if wanted[t]))
    return result


def seconds_until(warm_at: str, now: datetime) -> float:
    """Seconds from `now` (local, naive) to the next HH:MM; a time equal to now means tomorrow."""
    hour, minute = (int(x) for x in warm_at.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def run_schedule(
    tmdb: CatalogSource,
    services: dict[str, Service],
    warm_at: str,
    concurrency: int,
    metrics: Metrics,
    genres: list[str] | None = None,
    genre_aliases: dict[str, list[str]] | None = None,
    title_ids: TitleIdResolver | None = None,
    min_votes: int | None = None,
    genre_keywords: dict[str, list[str]] | None = None,
    daily: Callable[[], None] | None = None,
    kids_policy: KidsPolicy | None = None,
    kids_min_votes: int | None = None,
) -> None:
    """Warm now, then daily at `warm_at` local time. Runs until cancelled; never raises. `daily` (backup, cache
    pruning) runs after each warm, in a thread."""
    delay = 0.0
    while True:
        await asyncio.sleep(delay)
        started = asyncio.get_running_loop().time()
        try:
            r = await warm_detailed(
                tmdb, services, concurrency, genres, genre_aliases, title_ids,
                min_votes=min_votes, genre_keywords=genre_keywords, kids_policy=kids_policy,
                kids_min_votes=kids_min_votes,
            )  # fmt: skip
            metrics.log(
                "warm_done", titles=r.warmed, attempted=r.attempted, failed=r.failed,
                discover_failed=r.discover_failed, exact_failed=r.exact_failed, title_ids=r.title_ids,
                ms=round((asyncio.get_running_loop().time() - started) * 1000),
            )  # fmt: skip
        except asyncio.CancelledError:
            raise
        except Exception as e:
            metrics.log("warm_failed", level="WARNING", error=repr(e))
        if daily:
            try:
                await asyncio.to_thread(daily)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                metrics.log("daily_failed", level="WARNING", error=repr(e))
        delay = seconds_until(warm_at, datetime.now())
