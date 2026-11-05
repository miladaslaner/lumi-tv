from datetime import datetime

from lumitv import services as services_mod, warm as warm_mod
from lumitv.catalog_source import Details
from test_metrics import cfg  # noqa: F401
from test_tools import det


class FakeTmdb:
    """Each (provider, kind) discover returns 3 unique titles on page 1 and nothing after."""

    def __init__(self):
        self.exact_calls = []
        self.discover_calls, self.detail_calls, self.genre_calls, self.discover_genres = [], [], [], []

    async def genres(self, kind):
        self.genre_calls.append(kind)
        return {
            "movie": {"Comedy": 35, "Family": 10751},
            "tv": {"Comedy": 35, "Kids": 10762, "Family": 10751},
        }[kind]

    async def discover(
        self, kind, region, provider_ids, genres, certification_lte, page=1, any_genre=False, min_votes=None
    ):
        self.discover_calls.append((kind, region, tuple(provider_ids), page))
        self.discover_genres.append((kind, genres))
        if page == 2:
            return []
        base = 1000 * sorted(provider_ids)[0] + (500 if kind == "tv" else 0) + (genres[0] * 1_000_000 if genres else 0)
        return [{"id": base + i, "media_type": kind} for i in range(3)]

    async def details(self, tmdb_id, kind, exact_minutes=False) -> Details:
        (self.exact_calls if exact_minutes else self.detail_calls).append((tmdb_id, kind))
        return det("x")


async def test_warm_fetches_details_for_discovered_titles(cfg):  # noqa: F811
    for s in cfg.services:  # two services only: netflix, prime
        s.tmdb_provider_ids = s.tmdb_provider_ids if s.id in ("netflix", "prime") else []
    svc = services_mod.load(cfg)
    t = FakeTmdb()
    n = (await warm_mod.warm_detailed(t, svc, concurrency=3)).warmed
    assert n == 12 and len(t.detail_calls) == 12 and len(set(t.detail_calls)) == 12
    regions = {(c[1], c[2]) for c in t.discover_calls}
    assert regions == {("AE", (8,)), ("AE", (119,))}
    assert sorted(t.exact_calls) == sorted(c for c in t.detail_calls if c[1] == "tv") and len(t.exact_calls) == 6
    assert max(c[3] for c in t.discover_calls) == 2  # page 2 was empty, so paging stopped there


class FullPages(FakeTmdb):
    async def discover(
        self, kind, region, provider_ids, genres, certification_lte, page=1, any_genre=False, min_votes=None
    ):
        self.discover_calls.append((kind, region, tuple(provider_ids), page))
        return [
            {"id": 100000 * page + 1000 * sorted(provider_ids)[0] + (500 if kind == "tv" else 0), "media_type": kind}
        ]


async def test_warm_requests_pages_1_to_5_when_rows_keep_coming(cfg):  # noqa: F811
    for s in cfg.services:
        s.tmdb_provider_ids = s.tmdb_provider_ids if s.id == "netflix" else []
    t = FullPages()
    n = (await warm_mod.warm_detailed(t, services_mod.load(cfg))).warmed
    assert sorted(c[3] for c in t.discover_calls if c[0] == "movie") == [1, 2, 3, 4, 5]
    assert max(c[3] for c in t.discover_calls) == 5 and n == 10


async def test_warm_detailed_counts_failures(cfg):  # noqa: F811
    class Flaky(FakeTmdb):
        async def details(self, tmdb_id, kind, exact_minutes=False):
            if tmdb_id % 2:
                raise RuntimeError("x")
            return await super().details(tmdb_id, kind)

    r = await warm_mod.warm_detailed(Flaky(), services_mod.load(cfg))
    assert r.attempted == r.warmed + r.failed and r.failed > 0


async def test_warm_survives_failures(cfg):  # noqa: F811
    svc = services_mod.load(cfg)

    class Broken(FakeTmdb):
        async def details(self, tmdb_id, kind, exact_minutes=False):
            raise RuntimeError("boom")

    assert (await warm_mod.warm_detailed(Broken(), svc)).warmed == 0


def test_seconds_until_next_warm():
    assert warm_mod.seconds_until("17:00", datetime(2026, 11, 3, 16, 0)) == 3600
    assert warm_mod.seconds_until("17:00", datetime(2026, 11, 3, 17, 0)) == 24 * 3600
    assert warm_mod.seconds_until("17:00", datetime(2026, 11, 3, 18, 0)) == 23 * 3600


async def test_warm_fetches_genre_lists_and_survives_their_failure(cfg):  # noqa: F811
    svc = services_mod.load(cfg)
    t = FakeTmdb()
    await warm_mod.warm_detailed(t, svc)
    assert sorted(t.genre_calls) == ["movie", "tv"]

    class NoGenres(FakeTmdb):
        async def genres(self, kind):
            raise RuntimeError("down")

    assert (await warm_mod.warm_detailed(NoGenres(), svc)).warmed > 0


async def test_warm_requests_genre_filtered_discovers_with_aliases(cfg):  # noqa: F811
    svc = services_mod.load(cfg)
    t = FakeTmdb()
    aliases = {"kids": ["Kids", "Family"]}
    await warm_mod.warm_detailed(t, svc, genres=["comedy", "kids", "polka"], genre_aliases=aliases)
    genre_calls = {(k, tuple(g)) for k, g in t.discover_genres if g}
    assert genre_calls == {("movie", (35,)), ("tv", (35,)), ("movie", (10751,)), ("tv", (10762,))}
    # pages 1-2 (what the hours path reads), per service with providers, in that service's region
    # FakeTmdb.discover appends to both lists on every call
    filtered = [c for c, (_, g) in zip(t.discover_calls, t.discover_genres, strict=True) if g]
    assert {c[3] for c in filtered} == {1, 2} and {c[1] for c in filtered} == {"AE", "NL"}
    # their titles get their details warmed too
    assert any(i >= 35_000_000 for i, _ in t.detail_calls)


class ExactDown(FakeTmdb):
    async def details(self, tmdb_id, kind, exact_minutes=False):
        if exact_minutes:
            raise RuntimeError("seasons down")
        return await super().details(tmdb_id, kind)


async def test_warm_exact_failures_are_counted_not_raised(cfg):  # noqa: F811
    for s in cfg.services:
        s.tmdb_provider_ids = s.tmdb_provider_ids if s.id == "netflix" else []
    r = await warm_mod.warm_detailed(ExactDown(), services_mod.load(cfg))
    assert r.warmed == 6 and r.failed == 0 and r.exact_failed == 3


class FakeTitleIds:
    def __init__(self):
        self.resolved = []

    async def resolve(self, kind, tmdb_id, wanted):
        self.resolved.append((tmdb_id, kind, wanted))
        return {"google_id": "/m/0x"} if tmdb_id % 2 else {}


async def test_warm_resolves_title_ids_for_services_that_want_them(cfg):  # noqa: F811
    for s in cfg.services:  # netflix (wants netflix_id), prime (google_tv), videoland (nothing)
        s.tmdb_provider_ids = s.tmdb_provider_ids if s.id in ("netflix", "prime", "videoland") else []
    g = FakeTitleIds()
    r = await warm_mod.warm_detailed(FakeTmdb(), services_mod.load(cfg), concurrency=3, title_ids=g)
    ids = lambda p: [(p * 1000 + (500 if k == "tv" else 0) + i, k) for k in ("movie", "tv") for i in range(3)]  # noqa: E731
    want = {t: frozenset({"google_id"}) for t in ids(119)} | {t: frozenset({"netflix_id"}) for t in ids(8)}
    assert {(i, k): w for i, k, w in g.resolved} == want and len(g.resolved) == 12  # none for Videoland
    assert r.title_ids == 4  # odd ids resolve in the fake


class Recording(FakeTmdb):
    """Records each discover's full arguments, keywords included, to compare warm-up with asks."""

    def __init__(self):
        super().__init__()
        self.calls = []

    async def genres(self, kind):
        lists = await super().genres(kind)
        return {**lists, "Sci-Fi & Fantasy": 10765} if kind == "tv" else {**lists, "Science Fiction": 878}

    async def discover(
        self,
        kind,
        region,
        provider_ids,
        genres,
        certification_lte,
        page=1,
        any_genre=False,
        min_votes=None,
        keywords=None,
    ):
        self.calls.append(
            (
                kind,
                region,
                tuple(sorted(provider_ids)),
                tuple(genres or ()),
                certification_lte,
                page,
                any_genre,
                min_votes,
                tuple(keywords or ()),
            )
        )
        return await super().discover(kind, region, provider_ids, genres, certification_lte, page, any_genre, min_votes)

    async def keyword_ids(self, names):
        return {n: i for i, n in enumerate(names, start=1)}


async def test_warm_requests_the_discover_urls_asks_read(cfg):  # noqa: F811
    from lumitv.catalog import PickRegistry, find_titles
    from lumitv.config import Ranking

    svc = services_mod.load(cfg)
    aliases = {"sci-fi": ["Science Fiction", "Sci-Fi & Fantasy"]}
    keywords = {"sci-fi": ["space", "alien"]}
    t = Recording()
    await warm_mod.warm_detailed(t, svc, genres=["comedy", "sci-fi"], genre_aliases=aliases, min_votes=300,
                                 genre_keywords=keywords, kids_policy=cfg.kids_policy(), kids_min_votes=50)  # fmt: skip
    warmed = set(t.calls)
    # grown-up asks, and the kids asks the daytime default makes common: no age (cutoff "6"), a toddler ("AL"), an
    # age at the no-age cutoff, with and without a genre, with a length (page 2)
    asks = (
        dict(genres=["comedy"]),
        dict(genres=["sci-fi"], kind="tv"),
        dict(),
        dict(kids_mode=True),
        dict(kids_age=5, genres=["comedy"]),
        dict(kids_age=8, genres=["sci-fi"], kind="tv"),
        dict(kids_age=3),
        dict(kids_mode=True, max_hours=2),
    )
    for ask in asks:
        t.calls.clear()
        await find_titles(
            t,
            PickRegistry(),
            set(),
            svc,
            cfg.kids_policy(),
            genre_aliases=aliases,
            genre_keywords=keywords,
            ranking=Ranking(min_votes=300, kids_min_votes=50),
            **ask,
        )
        assert t.calls and set(t.calls) <= warmed, (ask, set(t.calls) - warmed)


async def test_without_a_kids_policy_the_kids_discovers_stay_cold(cfg):  # noqa: F811
    from lumitv.catalog import PickRegistry, find_titles
    from lumitv.config import Ranking

    svc = services_mod.load(cfg)
    t = Recording()
    await warm_mod.warm_detailed(t, svc, genres=["comedy"], min_votes=300)
    warmed = set(t.calls)
    t.calls.clear()
    await find_titles(t, PickRegistry(), set(), svc, cfg.kids_policy(), ranking=Ranking(min_votes=300), kids_mode=True)
    assert t.calls and not set(t.calls) & warmed  # every kids discover differs (cutoff, vote floor)
    t.calls.clear()
    await find_titles(t, PickRegistry(), set(), svc, cfg.kids_policy(), ranking=Ranking(min_votes=300))
    assert set(t.calls) <= warmed


async def test_daily_job_runs_after_each_warm_and_its_failure_is_logged(cfg):  # noqa: F811
    import asyncio

    from lumitv.metrics import Metrics

    ran = []

    def daily():
        ran.append(1)
        raise RuntimeError("disk full")

    logged = []
    m = Metrics(None)
    m.log = lambda event, **kw: logged.append(event)  # type: ignore[method-assign]
    task = asyncio.create_task(warm_mod.run_schedule(FakeTmdb(), services_mod.load(cfg), "03:00", 2, m, daily=daily))
    for _ in range(500):  # until the failure is logged, not just until the job started (slow machines)
        if "daily_failed" in logged:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert ran == [1] and "warm_done" in logged and "daily_failed" in logged
