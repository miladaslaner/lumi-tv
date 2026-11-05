import asyncio
import math
import time

import pytest

from lumitv.catalog import LookupStats, PickRegistry, _candidates, find_titles
from lumitv.catalog_source import Details, TmdbUnavailable
from lumitv.config import Ranking
from lumitv.kids import KidsPolicy
from lumitv.services import Service

SERVICES = {
    "netflix": Service("netflix", "Netflix", frozenset({8}), "AE", "pkg.nf", "off"),
    "prime": Service("prime", "Prime", frozenset({119}), "AE", "pkg.pv", "any"),
    "videoland": Service("videoland", "Videoland", frozenset({72}), "NL", "pkg.vl", "on"),
    "youtube": Service("youtube", "YouTube", frozenset(), "", "pkg.yt", "any"),
}
POLICY = KidsPolicy(
    order=["AL", "6", "9", "12", "16", "18"],
    toddler_below_age=4,
    toddler_allowed=["AL"],
    no_age_max="12",
)


def det(title="T", cert="12", flat=None, **kw):
    base = dict(
        title=title,
        year=2020,
        overview="o",
        poster_url="http://p",
        certification=cert,
        runtime_minutes=100,
        seasons=None,
        season1_minutes=None,
        total_minutes=None,
        flatrate_by_region={"AE": {8}} if flat is None else flat,
    )
    base.update(kw)
    return Details(**base)


class FakeSource:
    def __init__(
        self,
        details,
        kinds=None,
        delay=0.0,
        search_map=None,
        similar_map=None,
        discover_list=None,
        row_genres=None,
        discover_pages=None,
        row_meta=None,
    ):
        self.d = details
        self.row_genres = row_genres or {}  # tmdb id -> genre_ids carried on search rows, as TMDB does
        self.row_meta = row_meta or {}  # tmdb id -> extra row fields (vote_average, vote_count, genre_ids)
        self.min_votes_asked = []
        self.discover_pages = discover_pages  # page -> ids, overrides discover_list
        self.kinds = kinds or {}
        self.delay = delay
        self.search_map = search_map or {}
        self.similar_map = similar_map or {}
        self.discover_list = discover_list
        self.discover_calls = []
        self.discover_certs = []
        self.discover_genres = []
        self.genre_lists = {
            "movie": {
                "Comedy": 35,
                "Animation": 16,
                "Family": 10751,
                "Science Fiction": 878,
                "Horror": 27,
                "Action": 28,
                "Adventure": 12,
                "Fantasy": 14,
                "Thriller": 53,
                "Crime": 80,
                "Documentary": 99,
            },
            "tv": {
                "Comedy": 35,
                "Animation": 16,
                "Family": 10751,
                "Kids": 10762,
                "Sci-Fi & Fantasy": 10765,
                "Action & Adventure": 10759,
                "Crime": 80,
                "Documentary": 99,
            },
        }

    async def genres(self, kind):
        return self.genre_lists[kind]

    def _row(self, i):
        return {
            "id": i,
            "media_type": self.kinds.get(i, "movie"),
            "genre_ids": self.row_genres.get(i, []),
            **self.row_meta.get(i, {}),
        }

    async def search(self, query, kind):
        if isinstance(self.search_map.get(query), Exception):
            raise self.search_map[query]
        return [self._row(i) for i in self.search_map.get(query, [])]

    async def similar(self, tmdb_id, kind):
        return [self._row(i) for i in self.similar_map.get(tmdb_id, [])]

    async def discover(
        self, kind, region, provider_ids, genres, certification_lte, page=1, any_genre=False, min_votes=None
    ):
        self.discover_calls.append((kind, region, set(provider_ids)))
        self.discover_certs.append(certification_lte)
        self.discover_genres.append((kind, genres))
        self.discover_pages_asked = getattr(self, "discover_pages_asked", []) + [page]
        self.discover_any = getattr(self, "discover_any", []) + [any_genre]
        self.min_votes_asked.append(min_votes)
        if self.discover_pages is not None:
            ids = self.discover_pages.get(page, [])
        else:
            ids = self.discover_list if self.discover_list is not None else list(self.d)
        return [
            {"id": i, "media_type": kind, **self.row_meta.get(i, {})} for i in ids if self.kinds.get(i, "movie") == kind
        ]

    async def details(self, tmdb_id, kind, exact_minutes=False):
        if self.delay:
            await asyncio.sleep(self.delay)
        d = self.d[tmdb_id]
        if isinstance(d, Exception):
            raise d
        return d


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


async def run(src, seen=None, reg=None, **kw):
    return await find_titles(src, reg or PickRegistry(), seen or set(), SERVICES, POLICY, **kw)


async def test_only_flatrate_on_our_services():
    src = FakeSource(
        {1: det("Rent", flat={"AE": set()}), 2: det("Flat", flat={"AE": {8}}), 3: det("Wrongregion", flat={"NL": {8}})}
    )
    out = await run(src)
    assert [c.tmdb_id for c in out] == [2]
    assert out[0].services == ["netflix"]


async def test_service_filter():
    src = FakeSource({1: det(flat={"AE": {8}}), 2: det(flat={"NL": {72}}), 3: det(flat={"AE": {8}, "NL": {72}})})
    out = await run(src, service="videoland")
    assert sorted(c.tmdb_id for c in out) == [2, 3]
    assert all("videoland" in c.services for c in out)


async def test_kids_mode_drops_unrated_and_returns_fewer():
    d = {
        1: det(cert="AL"),
        2: det(cert="AL"),
        3: det(cert=None),
        4: det(cert=None),
        5: det(cert=None),
        6: det(cert="12"),
    }
    out = await run(FakeSource(d), kids_mode=True, kids_age=3)
    assert sorted(c.tmdb_id for c in out) == [1, 2]


async def test_hours_filter():
    d = {
        1: det(total_minutes=720, seasons=2),
        2: det(total_minutes=300, seasons=1),
        3: det(total_minutes=None, seasons=1),
        4: det(runtime_minutes=None),
    }
    kinds = {1: "tv", 2: "tv", 3: "tv"}
    out = await run(FakeSource(d, kinds), min_hours=10, max_hours=15)
    assert [c.tmdb_id for c in out] == [1]


async def test_hours_filter_movies_use_runtime():
    d = {1: det(runtime_minutes=90), 2: det(runtime_minutes=200)}
    out = await run(FakeSource(d), max_hours=2)
    assert [c.tmdb_id for c in out] == [1]


async def test_exclude_ids():
    out = await run(FakeSource({1: det(), 2: det(), 3: det()}), exclude_tmdb_ids=[1, 3])
    assert [c.tmdb_id for c in out] == [2]


def test_parse_excludes_takes_ids_and_kind_qualified_strings():
    from lumitv.catalog import parse_excludes

    assert parse_excludes([1, "2", "movie:3", "tv:3", " tv:4 "]) == ({1, 2}, {(3, "movie"), (3, "tv"), (4, "tv")})
    assert parse_excludes([]) == (set(), set())
    for bad in ("show:1", "tv:", "tv:0", "", "abc", "movie:1,tv:2"):
        with pytest.raises(ValueError, match="exclude_tmdb_ids"):
            parse_excludes([bad])


async def test_kind_qualified_excludes_leave_the_other_kind_alone():
    src = FakeSource({1: det(), 2: det()})
    assert [c.tmdb_id for c in await run(src, exclude_tmdb_ids=["movie:1"])] == [2]
    assert [c.tmdb_id for c in await run(src, exclude_tmdb_ids=["tv:1"])] == [
        1,
        2,
    ]  # a series with that id, not this film
    assert [c.tmdb_id for c in await run(src, exclude_tmdb_ids=["1"])] == [2]  # a bare id: both kinds


async def test_exclude_ids_hold_in_kids_mode_and_with_genres():
    d = {1: det("A", cert="AL"), 2: det("B", cert="AL"), 3: det("C", cert="AL")}
    assert [c.tmdb_id for c in await run(FakeSource(d), kids_age=5, exclude_tmdb_ids=[1, 3])] == [2]
    assert [c.tmdb_id for c in await run(FakeSource(d), genres=["comedy"], exclude_tmdb_ids=[2])] == [1, 3]
    assert [c.tmdb_id for c in await run(FakeSource(d), genres=["comedy"], kids_mode=True, exclude_tmdb_ids=[1])] == [
        2,
        3,
    ]


async def test_adult_titles_never_become_candidates_or_cards():
    # a row flagged adult (similar/recommendations have no include_adult switch) is skipped before any lookup, and
    # details that say adult are dropped even outside kids mode
    d = {1: det("Fine"), 2: det("Flagged row"), 3: det("Flagged details", adult=True)}
    src = Recording(d, search_map={"x": [1, 2, 3]}, row_meta={2: {"adult": True}})
    out = await run(src, query="x")
    assert [c.title for c in out] == ["Fine"]
    assert [i for i, _ in src.detail_calls] == [1, 3]  # the flagged row was never looked up
    plain = Recording(d, discover_list=[1, 2, 3], row_meta={2: {"adult": True}})
    assert [c.title for c in await run(plain)] == ["Fine"]


async def test_a_named_title_is_still_dropped_in_kids_mode():
    d = {1: det("Adult", cert="18"), 2: det("Unrated", cert=None), 3: det("Kid", cert="AL")}
    src = FakeSource(d, search_map={"adult": [1, 3], "unrated": [2]})
    assert [c.title for c in await run(src, query="adult", kids_age=3)] == ["Kid"]
    assert [c.title for c in await run(src, query="adult", kids_age=10)] == ["Kid"]
    assert await run(src, query="unrated", kids_mode=True) == []
    assert [c.title for c in await run(src, query="adult")] == ["Adult", "Kid"]  # outside kids mode, as asked


async def test_tokens_resolve_and_carry_kids_flags():
    reg = PickRegistry()
    out = await run(FakeSource({1: det(cert="AL")}), reg=reg, kids_mode=True, kids_age=3, turn_id="t1", question="q?")
    pick = reg.get(out[0].token)
    assert pick.kids_mode is True and pick.kids_age == 3
    assert pick.tmdb_id == 1 and pick.turn_id == "t1" and pick.question == "q?"
    assert len(out[0].token) == 8
    assert reg.get("nope") is None


async def test_token_expires_after_ttl():
    clock = Clock()
    reg = PickRegistry(clock=clock)
    out = await run(FakeSource({1: det()}), reg=reg)
    clock.t += 2 * 3600 - 1
    assert reg.get(out[0].token) is not None
    clock.t += 2
    assert reg.get(out[0].token) is None


async def test_missing_poster_kept():
    out = await run(FakeSource({1: det(poster_url=None)}))
    assert len(out) == 1 and out[0].poster_url is None


async def test_parallel_lookups_meet_budget():
    d = {i: det() for i in range(1, 21)}
    t0 = time.monotonic()
    out = await run(FakeSource(d, delay=0.2), concurrency=8, limit=20)
    assert len(out) == 20
    assert time.monotonic() - t0 < 2.0  # 3 rounds of 0.2 s in parallel; one by one would take 4 s


async def test_failed_detail_is_skipped():
    d = {1: det(), 2: TmdbUnavailable("down"), 3: RuntimeError("boom"), 4: det()}
    stats = LookupStats()
    out = await run(FakeSource(d), stats=stats)
    assert sorted(c.tmdb_id for c in out) == [1, 4]
    assert (stats.details_failed, stats.details_ok) == (2, 2) and stats.degraded() == ["lookups_failed:2"]


async def test_degraded_tells_an_outage_from_a_thin_result():
    stats = LookupStats()
    await run(FakeSource({1: det(), 2: det()}), stats=stats)
    assert stats.degraded() == [] and stats.inputs_ok == 6 and stats.details_ok == 2  # 3 services x 2 kinds discovered

    class Down(FakeSource):
        async def details(self, tmdb_id, kind, exact_minutes=False):
            raise TmdbUnavailable("down")

    stats = LookupStats()
    assert await run(Down({1: det(), 2: det()}), stats=stats) == []
    assert stats.degraded() == ["tmdb_unavailable"]  # the discovers worked, every detail lookup failed
    stats = LookupStats()
    assert await run(FakeSource({1: det()}, search_map={"x": RuntimeError("down")}), query="x", stats=stats) == []
    assert stats.degraded() == ["tmdb_unavailable"]  # the one input there was failed


async def test_seen_flag_and_limit_and_order():
    d = {i: det(title=f"Title {i}") for i in range(1, 6)}
    out = await run(FakeSource(d), seen={"title 2"}, limit=3)
    assert [c.tmdb_id for c in out] == [1, 2, 3]
    assert [c.seen for c in out] == [False, True, False]


async def test_like_and_query_candidates():
    d = {1: det("Liked"), 2: det("Sim"), 3: det("Q"), 4: det("Other")}
    src = FakeSource(d, search_map={"Liked": [1], "q": [3]}, similar_map={1: [2]})
    out = await run(src, like=["Liked"], query="q")
    assert sorted(c.tmdb_id for c in out) == [2, 3]  # the liked title itself is not suggested
    assert src.discover_calls == []


async def test_failing_search_does_not_abort():
    d = {1: det(), 3: det()}
    src = FakeSource(d, search_map={"bad": RuntimeError("x"), "q": [3]})
    out = await run(src, like=["bad"], query="q")
    assert [c.tmdb_id for c in out] == [3]


async def test_discover_runs_per_service_with_providers():
    src = FakeSource({1: det()})
    out = await run(src)
    regions = {(r, frozenset(p)) for _, r, p in src.discover_calls}
    assert regions == {("AE", frozenset({8})), ("AE", frozenset({119})), ("NL", frozenset({72}))}
    assert [c.tmdb_id for c in out] == [1]


async def test_kids_discover_receives_cutoff():
    # POLICY has no "14", so ages 15 and up (max_age 15) stop at "12"
    for age, expected in [(3, "AL"), (None, "12"), (9, "9"), (10, "9"), (15, "12"), (16, "12"), (99, "12")]:
        src = FakeSource({1: det(cert="AL")})
        await run(src, kids_mode=True, kids_age=age)
        assert set(src.discover_certs) == {expected}, age
    src = FakeSource({1: det()})
    await run(src)
    assert set(src.discover_certs) == {None}


async def test_catalog_caps_the_kids_age_itself():
    # scripts and tests call the catalogue directly: the ceiling must not depend on the tool layer
    d = {1: det("Sixteen", cert="16"), 2: det("Twelve", cert="12"), 3: det("Adult", cert="18")}
    reg = PickRegistry()
    out = await run(FakeSource(d), reg=reg, kids_age=99)
    assert [c.title for c in out] == ["Twelve"]
    assert reg.get(out[0].token).kids_age == 15  # the pick records the age that applied
    assert await run(FakeSource(d), kids_age=-3) == []  # a negative age fails closed: toddler rules, nothing AL here


async def test_like_ignores_kind_for_the_liked_search_but_filters_similar():
    d = {1: det("Liked"), 2: det("SimMovie"), 3: det("SimTv")}

    class Src(FakeSource):
        async def search(self, query, kind):
            assert kind is None
            return await super().search(query, kind)

        async def similar(self, tmdb_id, kind):
            return [self._row(2), self._row(3)]

    src = Src(d, kinds={1: "tv", 3: "tv"}, search_map={"Liked": [1]})
    out = await run(src, like=["Liked"], kind="tv", limit=1)
    assert [c.tmdb_id for c in out] == [3]


class Recording(FakeSource):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.detail_calls = []

    async def details(self, tmdb_id, kind, exact_minutes=False):
        self.detail_calls.append((tmdb_id, exact_minutes))
        return await super().details(tmdb_id, kind, exact_minutes)


async def test_candidate_cap_from_config_never_below_limit():
    d = {i: det() for i in range(1, 41)}
    src = Recording(d)
    await run(src, limit=3, candidates=7)
    assert len(src.detail_calls) == 7
    src = Recording(d)
    await run(src, limit=10, candidates=4)  # can't look up fewer than the caller wants
    assert len(src.detail_calls) == 10


async def test_exact_minutes_requested_only_with_hours_filter():
    src = Recording({1: det(), 2: det()})
    await run(src)
    assert all(not exact for _, exact in src.detail_calls)
    src = Recording({1: det(), 2: det()})
    await run(src, max_hours=3)  # movie runtimes are exact already: no second lookup
    assert all(not exact for _, exact in src.detail_calls)


ALIASES = {
    "sci-fi": ["Science Fiction", "Sci-Fi & Fantasy"],
    "kids": ["Kids", "Family"],
}


async def test_genres_map_per_kind_and_reach_discover():
    src = FakeSource({1: det()})
    stats = LookupStats()
    await run(src, genres=["Comedy", "animation"], stats=stats)
    assert set((k, tuple(g)) for k, g in src.discover_genres) == {("movie", (35, 16)), ("tv", (35, 16))}
    assert stats.unknown_genres == []


async def test_genre_aliases_resolve_differently_per_kind():
    src = FakeSource({1: det()})
    await run(src, genres=["sci-fi"], genre_aliases=ALIASES)
    assert set((k, tuple(g)) for k, g in src.discover_genres) == {("movie", (878,)), ("tv", (10765,))}
    src = FakeSource({1: det()})
    await run(src, genres=["kids"], genre_aliases=ALIASES)
    assert set((k, tuple(g)) for k, g in src.discover_genres) == {("movie", (10751,)), ("tv", (10762,))}


async def test_genre_not_in_one_kind_skips_that_kind():
    src = FakeSource({1: det()})
    await run(src, genres=["horror"])  # movies only
    assert {k for k, _ in src.discover_genres} == {"movie"}
    src = FakeSource({1: det(), 2: det()}, kinds={2: "tv"})
    await run(src, genres=["horror"], kind="tv")  # asked for tv, tv has no horror
    assert src.discover_genres == []


async def test_unknown_genres_reported_and_ignored():
    src = FakeSource({1: det()})
    stats = LookupStats()
    out = await run(src, genres=["comedy", "polka"], stats=stats)
    assert stats.unknown_genres == ["polka"]
    assert {tuple(g) for _, g in src.discover_genres} == {(35,)}
    assert [c.tmdb_id for c in out] == [1]


async def test_kids_mode_with_genres_passes_cutoff_and_genres():
    src = FakeSource({1: det(cert="AL")})
    await run(src, genres=["animation"], kids_mode=True, kids_age=3)
    assert src.discover_certs and all(c == "AL" for c in src.discover_certs)
    assert {tuple(g) for _, g in src.discover_genres} == {(16,)}


async def test_genre_candidates_come_before_query_candidates():
    src = FakeSource({1: det("Query"), 2: det("Genre")}, search_map={"x": [1]}, discover_list=[2])
    out = await run(src, genres=["comedy"], query="x")
    assert [c.tmdb_id for c in out] == [2, 1]


async def test_query_alone_does_not_fetch_genres_or_discover():
    src = FakeSource({1: det()}, search_map={"x": [1]})
    await run(src, query="x")
    assert src.discover_genres == []


async def test_genre_list_failure_is_counted_not_fatal():
    class Down(FakeSource):
        async def genres(self, kind):
            raise TmdbUnavailable("down")

    stats = LookupStats()
    out = await run(Down({1: det()}), genres=["comedy"], stats=stats)
    assert out == [] and stats.inputs_failed == 2
    assert stats.unknown_genres == [] and stats.skipped_kinds == []  # not blamed on the user's genre names
    assert stats.degraded() == ["tmdb_unavailable"]


async def test_skipped_kinds_reported():
    src = FakeSource({1: det()})
    stats = LookupStats()
    await run(src, genres=["horror"], stats=stats)
    assert stats.skipped_kinds == ["tv"]
    stats = LookupStats()
    await run(FakeSource({1: det()}), genres=["comedy"], stats=stats)
    assert stats.skipped_kinds == []


BRIDGE_ALIASES = {
    "science fiction": ["Science Fiction", "Sci-Fi & Fantasy"],
    "fantasy": ["Fantasy", "Sci-Fi & Fantasy"],
    "action": ["Action", "Action & Adventure"],
    "adventure": ["Adventure", "Action & Adventure"],
}


async def test_cross_kind_like_bridges_genres_to_requested_kind():
    # a liked film, asked for series: TMDB similar stays in the film's kind, so genres bridge to tv discover
    d = {1: det("Matrix"), 2: det("SimFilm"), 7: det("TvA"), 8: det("TvB")}
    src = FakeSource(
        d,
        kinds={7: "tv", 8: "tv"},
        search_map={"Matrix": [1]},
        similar_map={1: [2]},
        row_genres={1: [28, 878, 53]},
        discover_list=[7, 8],
    )
    out = await run(src, like=["Matrix"], kind="tv", genre_aliases=BRIDGE_ALIASES)
    assert [c.tmdb_id for c in out] == [7, 8] and all(c.kind == "tv" for c in out)
    assert {k for k, _ in src.discover_genres} == {"tv"}
    assert {tuple(g) for _, g in src.discover_genres} == {(10759, 10765)}  # thriller has no tv genre: dropped
    assert all(src.discover_any)  # OR-joined


async def test_bridged_genres_most_frequent_first():
    d = {1: det("A"), 2: det("B"), 3: det("C"), 7: det("Tv")}
    src = FakeSource(
        d,
        kinds={7: "tv"},
        search_map={"A": [1], "B": [2], "C": [3]},
        row_genres={1: [28, 878], 2: [14, 12, 28], 3: [878]},
        discover_list=[7],
    )
    await run(src, like=["A", "B", "C"], kind="tv", genre_aliases=BRIDGE_ALIASES)
    # sci-fi/fantasy: A, B, C; action/adventure: A, B (B counts once though it has two of them)
    assert {tuple(g) for _, g in src.discover_genres} == {(10765, 10759)}


async def test_same_kind_similar_ranked_before_bridged():
    d = {1: det("LikedTv"), 2: det("LikedFilm"), 5: det("SimTv"), 6: det("Bridged")}
    src = FakeSource(
        d,
        kinds={1: "tv", 5: "tv", 6: "tv"},
        search_map={"x": [1], "y": [2]},
        similar_map={1: [5]},
        row_genres={1: [10765], 2: [878]},
        discover_list=[6, 5],
    )
    out = await run(src, like=["x", "y"], kind="tv", genre_aliases=BRIDGE_ALIASES)
    assert [c.tmdb_id for c in out] == [5, 6]


async def test_same_kind_like_bridges_only_when_similar_is_short():
    d = {1: det("Liked"), **{i: det(f"S{i}") for i in range(10, 14)}, 20: det("Bridged")}
    kinds = {i: "tv" for i in d}
    src = FakeSource(
        d,
        kinds=kinds,
        search_map={"x": [1]},
        similar_map={1: [10, 11, 12, 13]},
        row_genres={1: [10765]},
        discover_list=[20],
    )
    await run(src, like=["x"], kind="tv", limit=3, genre_aliases=BRIDGE_ALIASES)
    assert src.discover_calls == []  # 4 same-kind similar >= limit 3
    src = FakeSource(
        d,
        kinds=kinds,
        search_map={"x": [1]},
        similar_map={1: [10, 11, 12, 13]},
        row_genres={1: [10765]},
        discover_list=[20],
    )
    out = await run(src, like=["x"], kind="tv", limit=10, genre_aliases=BRIDGE_ALIASES)
    assert {tuple(g) for _, g in src.discover_genres} == {(10765,)}
    assert [c.tmdb_id for c in out] == [
        10,
        11,
        12,
        13,
        20,
    ]  # scored: similar (2 each) before the bridged-only title (1)


async def test_like_without_kind_bridges_both_kinds_even_when_similar_looks_full():
    # Spider-Man: 8 similar rows (more than the limit), none on our services. Their count says nothing about
    # how many survive the service check, so with no kind the liked genres always bridge to movie and tv.
    d = {
        1: det("SpiderMan"),
        5: det("FilmOnUs"),
        6: det("SeriesOnUs"),
        **{i: det(f"Marvel{i}", flat={}) for i in range(10, 18)},
    }
    src = FakeSource(
        d,
        kinds={6: "tv"},
        search_map={"Spider-Man": [1]},
        similar_map={1: list(range(10, 18))},
        row_genres={1: [28, 878, 12]},
        discover_list=[5, 6],
    )
    out = await run(src, like=["Spider-Man"], limit=3, genre_aliases=BRIDGE_ALIASES, ranking=rk(set(), 300))
    assert [c.tmdb_id for c in out] == [5, 6]
    assert {k for k, _ in src.discover_genres} == {"movie", "tv"}
    assert {tuple(g) for k, g in src.discover_genres if k == "movie"} == {(28, 878, 12)}
    assert {tuple(g) for k, g in src.discover_genres if k == "tv"} == {(10759, 10765)}  # per-kind ids
    assert all(src.discover_any) and set(src.min_votes_asked) == {300}


async def test_like_without_kind_bridge_skips_exclude_and_liked():
    d = {1: det("Liked"), 5: det("A"), 6: det("B")}
    src = FakeSource(d, search_map={"x": [1]}, row_genres={1: [28]}, discover_list=[1, 5, 6])
    out = await run(src, like=["x"], exclude_tmdb_ids=[5], genre_aliases=BRIDGE_ALIASES)
    assert [c.tmdb_id for c in out] == [6]  # never the liked title, never an excluded one


async def test_like_without_kind_never_pads_when_bridge_is_empty():
    src = FakeSource({1: det("Liked")}, search_map={"x": [1]}, row_genres={1: [28]}, discover_list=[])
    assert await run(src, like=["x"], limit=6, genre_aliases=BRIDGE_ALIASES) == []


async def test_like_without_kind_in_kids_mode_keeps_kids_rules():
    d = {1: det("Film"), 7: det("TvOk", cert="AL"), 8: det("TvOld", cert="12"), 9: det("FilmOk", cert="AL")}
    src = FakeSource(d, kinds={7: "tv", 8: "tv"}, search_map={"f": [1]}, row_genres={1: [12]}, discover_list=[7, 8, 9])
    out = await run(src, like=["f"], kids_mode=True, kids_age=3, genre_aliases=BRIDGE_ALIASES, ranking=rk(set(), 300))
    assert set(src.discover_certs) == {"AL"} and set(src.min_votes_asked) == {None}
    assert {c.tmdb_id for c in out} == {7, 9}


async def test_lighter_question_drops_the_icarly_revival_but_not_comedies():
    # real TMDB genre ids: Matrix/LOTR/Star Trek carry no Family; iCarly (2021, tv 119243) is Comedy + Family.
    # (The 2007 iCarly, tv 5371, is tagged Comedy only on TMDB, so no genre rule can tell it from a sitcom.)
    d = {1: det("Matrix"), 2: det("LOTR"), 3: det("Star Trek"), 119243: det("iCarly"), 2316: det("The Office")}
    meta = {119243: votes(7.4, 900, 35, 10751), 2316: votes(8.6, 4000, 35)}
    src = FakeSource(
        d,
        kinds={3: "tv", 119243: "tv", 2316: "tv"},
        search_map={"Matrix": [1], "LOTR": [2], "Star Trek": [3]},
        row_genres={1: [28, 878], 2: [12, 14, 28], 3: [10765, 18]},
        discover_list=[119243, 2316],
        row_meta=meta,
    )
    out = await run(
        src,
        like=["Matrix", "LOTR", "Star Trek"],
        genres=["comedy"],
        limit=6,
        genre_aliases=BRIDGE_ALIASES,
        ranking=Ranking(),
    )
    assert [c.tmdb_id for c in out] == [2316]


async def test_cross_kind_like_in_kids_mode_passes_cutoff_and_filters():
    d = {1: det("Film"), 7: det("TvOk", cert="AL"), 8: det("TvOld", cert="12")}
    src = FakeSource(d, kinds={7: "tv", 8: "tv"}, search_map={"f": [1]}, row_genres={1: [12]}, discover_list=[7, 8])
    out = await run(src, like=["f"], kind="tv", kids_mode=True, kids_age=3, genre_aliases=BRIDGE_ALIASES)
    assert set(src.discover_certs) == {"AL"}
    assert [c.tmdb_id for c in out] == [7]


def tv(total, **kw):
    return det(total_minutes=total, seasons=1, **kw)


async def test_hours_filter_looks_past_base_cap_and_deeper_pages():
    # page 1: four long series (out of range); page 2: two in range. Base cap 4 would see only page 1.
    d = {1: tv(3000), 2: tv(3000), 3: tv(3000), 4: tv(3000), 5: tv(700), 6: tv(800)}
    src = Recording(d, kinds={i: "tv" for i in d}, discover_pages={1: [1, 2, 3, 4], 2: [5, 6]})
    out = await run(src, kind="tv", min_hours=10, max_hours=15, candidates=4, hours_candidates=8, limit=3)
    assert [c.tmdb_id for c in out] == [5, 6]
    assert set(src.discover_pages_asked) == {1, 2}
    src = Recording(d, kinds={i: "tv" for i in d}, discover_pages={1: [1, 2, 3, 4], 2: [5, 6]})
    await run(src, kind="tv", candidates=4, hours_candidates=8, limit=3)
    assert set(src.discover_pages_asked) == {1}  # no hours filter: page 1 only


async def test_hours_pool_capped_by_hours_candidates():
    d = {i: tv(700) for i in range(1, 31)}
    src = Recording(d, kinds={i: "tv" for i in d}, discover_pages={1: list(range(1, 16)), 2: list(range(16, 31))})
    await run(src, kind="tv", min_hours=10, max_hours=15, candidates=4, hours_candidates=8, limit=3)
    assert len({i for i, _ in src.detail_calls}) == 8


class EstimateSource(Recording):
    """details() returns the cheap estimate unless exact_minutes is asked for."""

    def __init__(self, estimates, exact, **kw):
        super().__init__({i: tv(m) for i, m in estimates.items()}, kinds={i: "tv" for i in estimates}, **kw)
        self.exact = {i: tv(m) for i, m in exact.items()}

    async def details(self, tmdb_id, kind, exact_minutes=False):
        d = await super().details(tmdb_id, kind, exact_minutes)
        return self.exact[tmdb_id] if exact_minutes else d


async def test_exact_minutes_only_for_estimate_passing_candidates():
    estimates = {1: 720, 2: 2000, 3: 500, 4: 500, 5: None, 6: 200}
    exact = {1: 700, 2: 2000, 3: 650, 4: 500, 5: 800, 6: 200}
    src = EstimateSource(estimates, exact)
    out = await run(src, kind="tv", min_hours=10, max_hours=15)
    exact_ids = sorted(i for i, e in src.detail_calls if e)
    assert exact_ids == [1, 3, 4, 5]  # 2 and 6 are far outside 10-15 h (beyond the margin); 5 has no estimate
    assert [c.tmdb_id for c in out] == [1, 3, 5]
    assert all(600 <= c.total_minutes <= 900 for c in out)


async def test_exact_minutes_skipped_for_titles_not_on_our_services():
    src = EstimateSource({1: 700}, {1: 700})
    src.d[1] = tv(700, flat={"AE": set()})
    await run(src, kind="tv", min_hours=10, max_hours=15)
    assert [e for _, e in src.detail_calls] == [False]


async def test_bridged_genres_keep_the_shared_ones():
    # A and B are sci-fi; only C is a thriller-ish fantasy: OR-ing every genre would flood the results
    d = {1: det("A"), 2: det("B"), 3: det("C"), 7: det("Tv")}
    src = FakeSource(
        d,
        kinds={7: "tv"},
        search_map={"A": [1], "B": [2], "C": [3]},
        row_genres={1: [878], 2: [878], 3: [14, 35]},
        discover_list=[7],
    )
    await run(src, like=["A", "B", "C"], kind="tv", genre_aliases=BRIDGE_ALIASES)
    assert {tuple(g) for _, g in src.discover_genres} == {(10765,)}
    src = FakeSource(
        d, kinds={7: "tv"}, search_map={"A": [1], "C": [3]}, row_genres={1: [878], 3: [35]}, discover_list=[7]
    )
    await run(src, like=["A", "C"], kind="tv", genre_aliases=BRIDGE_ALIASES)  # nothing shared: all of them
    assert {tuple(g) for _, g in src.discover_genres} == {(10765, 35)}


async def test_bridged_candidates_not_starved_by_similar():
    # similar isn't limited to our services; bridged discover is, so it must reach the capped pool
    d = {
        1: det("LikedTv"),
        2: det("Film"),
        **{i: det(f"S{i}", flat={"AE": set()}) for i in range(10, 30)},
        50: det("Bridged"),
    }
    kinds = {i: "tv" for i in d if i != 2}
    src = FakeSource(
        d,
        kinds=kinds,
        search_map={"x": [1], "y": [2]},
        similar_map={1: list(range(10, 30))},
        row_genres={1: [10765], 2: [878]},
        discover_list=[50],
    )
    out = await run(src, like=["x", "y"], kind="tv", candidates=5)
    assert [c.tmdb_id for c in out] == [50]


def ser(total, s1=None, s2=None, **kw):
    return det(total_minutes=total, season1_minutes=s1, season2_minutes=s2, seasons=3, **kw)


class SpanSource(Recording):
    """details() returns the estimate unless exact_minutes is asked for."""

    def __init__(self, estimates, exact):
        super().__init__(estimates, kinds={i: "tv" for i in estimates})
        self.exact = exact

    async def details(self, tmdb_id, kind, exact_minutes=False):
        d = await super().details(tmdb_id, kind, exact_minutes)
        return self.exact[tmdb_id] if exact_minutes else d


async def test_series_matches_whole_run_first_then_season_one_then_two_seasons():
    d = {
        1: ser(700, 100, 100),  # whole run fits
        2: ser(3000, 700, 200),  # season 1 fits
        3: ser(3000, 300, 400),  # seasons 1-2 fit
        4: ser(3000, 100, 100),  # nothing fits
        5: ser(3000, 700, 100),  # season 1 wins over seasons 1-2 (800 also fits)
    }
    src = Recording(d, kinds={i: "tv" for i in d})
    out = await run(src, kind="tv", min_hours=10, max_hours=15, limit=10)
    got = {c.tmdb_id: (c.binge_seasons, c.binge_minutes) for c in out}
    assert got == {1: (None, 700), 2: (1, 700), 3: (2, 700), 5: (1, 700)}


async def test_two_season_span_needs_season_two_minutes():
    src = Recording({1: ser(3000, 300, None)}, kinds={1: "tv"})
    assert await run(src, kind="tv", min_hours=10, max_hours=15) == []


async def test_exact_lookup_for_series_whose_first_seasons_may_fit():
    estimates = {1: ser(3000, 700, 700), 2: ser(3000, 100, 100), 3: ser(3000, 200, 400), 4: ser(None, None, None)}
    exact = {1: ser(3100, 650, 700), 2: ser(3000, 100, 100), 3: ser(3000, 250, 500), 4: ser(3000, 800, 700)}
    src = SpanSource(estimates, exact)
    out = await run(src, kind="tv", min_hours=10, max_hours=15, limit=10)
    assert sorted(i for i, e in src.detail_calls if e) == [1, 3, 4]  # 2 is far outside on every span
    assert {c.tmdb_id: (c.binge_seasons, c.binge_minutes) for c in out} == {1: (1, 650), 3: (2, 750), 4: (1, 800)}


async def test_no_binge_span_without_hours_filter_and_movies_unchanged():
    d = {1: ser(3000, 700, 100), 2: det("Film", runtime_minutes=700)}
    src = Recording(d, kinds={1: "tv"})
    out = await run(src, min_hours=None)
    assert all(c.binge_seasons is None and c.binge_minutes is None for c in out)
    assert all(not e for _, e in src.detail_calls)
    out = await run(Recording(d, kinds={1: "tv"}), kind="movie", min_hours=10, max_hours=15)
    assert [(c.tmdb_id, c.binge_seasons, c.binge_minutes) for c in out] == [(2, None, None)]


async def test_kids_rules_hold_on_the_hours_path():
    # "a film for the kids, under 3 hours": the length branch has its own lookups and exits; the kids filter still
    # decides, and ineligible series don't even get the exact season lookup
    d = {
        1: det("Unrated", cert=None, runtime_minutes=90),
        2: det("Grown", cert="16", runtime_minutes=90),
        3: det("Kid", cert="AL", runtime_minutes=90),
        4: ser(300, 150, 100, title="KidShow", cert="AL"),
        5: ser(300, 150, 100, title="GrownShow", cert="16"),
    }
    src = Recording(d, kinds={4: "tv", 5: "tv"})
    out = await run(src, kids_age=5, max_hours=3, limit=10)
    assert {c.title: c.binge_seasons for c in out} == {"Kid": None, "KidShow": 1}
    assert [i for i, exact in src.detail_calls if exact] == [4]


def test_most_recent_pick_wins_when_clock_ties():
    reg = PickRegistry(clock=lambda: 100.0)  # frozen clock: every issued_at is equal
    card = dict(
        tmdb_id=1,
        kind="movie",
        title="A",
        year=2020,
        overview="",
        poster_url=None,
        services=["netflix"],
        certification=None,
        minutes=90,
        total_minutes=None,
        seasons=None,
        seen=False,
    )
    t1 = reg.issue(card, False, None)
    t2 = reg.issue({**card, "tmdb_id": 2}, False, None)
    p1, p2 = reg.get(t1), reg.get(t2)
    assert p1.issued_at == p2.issued_at
    assert max([p1, p2], key=lambda p: p.seq) is p2


# ---- ranking of "like" questions -----------------------------------------------------------------


def rk(off=(), min_votes=0, **kw):
    return Ranking(off_genres_unless_liked=list(off), min_votes=min_votes, **kw)


def votes(avg, count, *genre_ids):
    return {"vote_average": avg, "vote_count": count, "genre_ids": list(genre_ids)}


async def test_like_ranks_by_overlap_then_bridged_then_quality():
    # 10 is in both liked titles' lists (4), 11/12 in one (2), 20 and 21 only bridged (1)
    d = {i: det(f"T{i}") for i in (1, 2, 10, 11, 12, 20, 21)}
    kinds = {i: "tv" for i in d}
    src = FakeSource(
        d,
        kinds=kinds,
        search_map={"a": [1], "b": [2]},
        similar_map={1: [11, 10, 12], 2: [10]},
        row_genres={1: [10765], 2: [10765]},
        discover_list=[21, 20],
        row_meta={11: votes(6.0, 50), 12: votes(8.0, 5000), 20: votes(7.0, 300), 21: votes(9.0, 4000)},
    )
    out = await run(src, like=["a", "b"], kind="tv", limit=10, genre_aliases=BRIDGE_ALIASES)
    # tie within a score is broken by Bayesian quality: 12 (8.0 x 5000) over 11 (6.0 x 50); 21 over 20
    assert [c.tmdb_id for c in out] == [10, 12, 11, 21, 20]


async def test_bayesian_weighting_discounts_few_votes():
    d = {i: det(f"T{i}") for i in (1, 2, 3)}
    src = FakeSource(
        d,
        kinds={i: "tv" for i in d},
        search_map={"a": [1]},
        similar_map={1: [2, 3]},
        row_meta={2: votes(10.0, 3), 3: votes(7.5, 2000)},
    )
    out = await run(src, like=["a"], kind="tv")
    assert [c.tmdb_id for c in out] == [3, 2]  # a perfect 10 from 3 votes is not trusted


async def test_off_genres_dropped_unless_liked_or_requested():
    off = frozenset({16, 10751})
    d = {i: det(f"T{i}") for i in (1, 10, 11, 12)}
    meta = {10: votes(7, 900, 18), 11: votes(7, 900, 16), 12: votes(7, 900, 10751, 18)}
    kw = dict(like=["a"], limit=10, ranking=rk(off))
    src = FakeSource(d, search_map={"a": [1]}, similar_map={1: [10, 11, 12]}, row_genres={1: [28]}, row_meta=meta)
    assert [c.tmdb_id for c in await run(src, **kw)] == [10]
    liked_anim = FakeSource(
        d, search_map={"a": [1]}, similar_map={1: [10, 11, 12]}, row_genres={1: [16]}, row_meta=meta
    )
    assert {c.tmdb_id for c in await run(liked_anim, **kw)} == {
        10,
        11,
    }  # liking an animation keeps animation, not Family
    requested = FakeSource(d, discover_list=[10, 11, 12], row_meta=meta)
    got = await run(requested, genres=["animation"], limit=10, ranking=rk(off))
    assert {c.tmdb_id for c in got} == {10, 11}  # a requested genre (animation) is kept, Family still goes
    plain = FakeSource(d, discover_list=[10, 11, 12], row_meta=meta)
    assert [c.tmdb_id for c in await run(plain, limit=10, ranking=rk(off))] == [10]


async def test_off_genre_filter_spares_a_named_title():
    d = {1: det("Bluey")}
    src = FakeSource(d, search_map={"bluey": [1]}, row_meta={1: votes(8, 900, 16)})
    assert [c.tmdb_id for c in await run(src, query="bluey", ranking=rk({16}))] == [1]


async def test_kids_mode_keeps_animation_and_kids_genres():
    d = {i: det(f"T{i}", cert="AL") for i in (1, 10, 11)}
    meta = {10: votes(7, 900, 16), 11: votes(7, 900, 10762)}
    src = FakeSource(d, search_map={"a": [1]}, similar_map={1: [10, 11]}, row_genres={1: [18]}, row_meta=meta)
    out = await run(src, like=["a"], kids_mode=True, kids_age=4, limit=10, ranking=rk({16, 10762, 10751}))
    assert {c.tmdb_id for c in out} == {10, 11}


async def test_min_votes_on_bridged_and_genre_discover():
    d = {1: det("Matrix"), 2: det("Sim"), 7: det("Tv")}
    src = FakeSource(
        d, kinds={7: "tv"}, search_map={"Matrix": [1]}, similar_map={1: [2]}, row_genres={1: [878]}, discover_list=[7]
    )
    await run(src, like=["Matrix"], kind="tv", genre_aliases=BRIDGE_ALIASES, ranking=rk(set(), 300))
    assert set(src.min_votes_asked) == {300}  # the bridged discover
    plain = FakeSource(d, kinds={7: "tv"}, discover_list=[7])
    await run(plain, genres=["comedy"], kind="tv", ranking=rk(set(), 300))
    assert set(plain.min_votes_asked) == {300}  # genre discover too: barely rated new releases don't lead
    kids = FakeSource({7: det("Tv", cert="AL")}, kinds={7: "tv"}, discover_list=[7])
    await run(kids, genres=["comedy"], kind="tv", kids_age=5, ranking=rk(set(), 300))
    assert set(kids.min_votes_asked) == {50}  # ranking.kids_min_votes: kids' titles get fewer votes
    kids = FakeSource({7: det("Tv", cert="AL")}, kinds={7: "tv"}, discover_list=[7])
    await run(kids, genres=["comedy"], kind="tv", kids_age=5, ranking=rk(set(), 300, kids_min_votes=0))
    assert set(kids.min_votes_asked) == {None}  # 0 = no floor


async def test_like_with_genres_puts_requested_genre_first():
    # similar titles 10, 11 are not comedy; comedy discover gives 30, 31. Requested genre leads, as before scoring.
    d = {i: det(f"T{i}") for i in (1, 10, 11, 30, 31)}
    src = FakeSource(
        d,
        search_map={"a": [1]},
        similar_map={1: [10, 11]},
        discover_list=[30, 31],
        row_meta={10: votes(9, 5000), 11: votes(9, 5000), 31: votes(5, 10)},
    )
    out = await run(src, like=["a"], genres=["comedy"], limit=3)
    assert [c.tmdb_id for c in out] == [30, 31, 10]


async def test_scoring_orders_within_the_genre_group():
    # 31 also sits in the liked title's similar list: it leads the genre group
    d = {i: det(f"T{i}") for i in (1, 10, 30, 31)}
    src = FakeSource(d, search_map={"a": [1]}, similar_map={1: [10, 31]}, discover_list=[30, 31])
    out = await run(src, like=["a"], genres=["comedy"], limit=4)
    assert [c.tmdb_id for c in out] == [31, 30, 10]


async def test_kids_mode_unchanged_by_scoring_and_min_votes():
    # cross-kind like in kids mode: plain interleave order (similar-less, bridged), no vote floor, no reserve
    d = {1: det("Frozen", cert="AL"), **{i: det(f"K{i}", cert="AL") for i in (7, 8, 9)}}
    meta = {7: votes(5, 10), 8: votes(9, 9000), 9: votes(7, 500)}
    src = FakeSource(
        d,
        kinds={7: "tv", 8: "tv", 9: "tv"},
        search_map={"Frozen": [1]},
        row_genres={1: [16]},
        discover_list=[7, 8, 9],
        row_meta=meta,
    )
    out = await run(
        src, like=["Frozen"], kind="tv", kids_mode=True, kids_age=4, genre_aliases=BRIDGE_ALIASES, ranking=rk({16}, 300)
    )
    assert [c.tmdb_id for c in out] == [7, 8, 9]  # not re-sorted by quality
    assert set(src.min_votes_asked) == {None}


async def test_reserve_keeps_a_share_of_the_pool_for_bridged_titles():
    # cap 4: 6 similar titles score 2 and outrank 3 bridged-only (1); discover_share of the slots stays reserved
    sims = list(range(10, 16))
    d = {1: det("L"), **{i: det(f"S{i}") for i in sims}, **{i: det(f"B{i}") for i in (50, 51, 52)}}

    async def pool(**kw):
        src = FakeSource(
            d,
            kinds={i: "tv" for i in d},
            search_map={"x": [1]},
            similar_map={1: sims},
            row_genres={1: [10765]},
            discover_list=[50, 51, 52],
        )
        got = await _candidates(
            src,
            SERVICES,
            None,
            ["x"],
            None,
            "tv",
            POLICY,
            False,
            None,
            genre_aliases=BRIDGE_ALIASES,
            limit=20,
            cap=4,
            **kw,
        )
        return [c[0] for c in got]

    assert await pool() == [10, 50, 51, 52]  # default 0.75: three of four slots
    assert await pool(ranking=rk(discover_share=0.5)) == [10, 11, 50, 51]


async def test_excluded_ids_do_not_use_up_the_cap():
    d = {i: det(f"T{i}") for i in (1, 10, 11, 12)}
    src = FakeSource(d, search_map={"a": [1]}, similar_map={1: [10, 11, 12]})
    out = await run(src, like=["a"], limit=2, candidates=2, exclude_tmdb_ids=[10])
    assert [c.tmdb_id for c in out] == [11, 12]  # 10 is dropped before the 2-title pool is cut


async def test_requested_service_is_the_picks_service():
    src = FakeSource({1: det(flat={"AE": {8, 119}})})
    both = await run(src)
    assert both[0].services == ["netflix", "prime"] and both[0].service == "netflix"  # config order
    asked = await run(src, service="prime")
    assert asked[0].services == ["prime", "netflix"] and asked[0].service == "prime"


# ---- Task 15: genre fit, documentaries, liked-title choice, blocklist ------------------------------


async def test_spider_man_picks_follow_genre_fit_not_a_stray_recommendation():
    # Live cause (real TMDB genre ids): Spider-Man's recommendations hold Kill Bill: Vol. 2 (Action, Crime, Thriller),
    # which is also in the bridged discover, so overlap 2 + bridged 1 put it first; Facing El Chapo (Crime, Action,
    # Thriller) came in on the Action bridge. Neither shares much beyond Action with Spider-Man (Action, Sci-Fi).
    spider, kill_bill, el_chapo, hunger, terminator, boys = 557, 393, 1621552, 101299, 218, 76479
    marvel = [299536, 24428]  # recommended, not on our services
    d = {
        spider: det("Spider-Man"),
        kill_bill: det("Kill Bill: Vol. 2"),
        el_chapo: det("Facing El Chapo"),
        hunger: det("Catching Fire"),
        terminator: det("The Terminator"),
        boys: det("The Boys"),
        **{m: det(f"Marvel{m}", flat={}) for m in marvel},
    }
    meta = {
        kill_bill: votes(7.9, 15628, 28, 80, 53),
        el_chapo: votes(8.4, 973, 80, 28, 53),
        hunger: votes(7.4, 18000, 12, 28, 878),
        terminator: votes(7.7, 13000, 28, 53, 878),
        boys: votes(8.4, 13625, 10765, 10759),
        **{m: votes(8.0, 30000, 12, 28, 878) for m in marvel},
    }
    src = FakeSource(
        d,
        kinds={boys: "tv"},
        search_map={"Spider-Man": [spider]},
        similar_map={spider: [*marvel, kill_bill]},
        row_genres={spider: [28, 878]},
        discover_list=[el_chapo, kill_bill, hunger, terminator, boys],
        row_meta=meta,
    )
    out = await run(src, like=["Spider-Man"], limit=3, genre_aliases=BRIDGE_ALIASES, ranking=Ranking())
    assert [c.tmdb_id for c in out] == [boys, terminator, hunger]
    # without the genre term the old order comes back: Kill Bill leads on overlap
    old = await run(src, like=["Spider-Man"], limit=3, genre_aliases=BRIDGE_ALIASES, ranking=Ranking(genre_weight=0))
    assert old[0].tmdb_id == kill_bill


async def test_genre_fit_is_relative_within_a_kind():
    # tv genres are coarser: Action + Adventure are one tv genre, so a series matches a liked film "perfectly"
    # (1.0) while the best film reaches 2/3. Each is measured against its kind's best, so quality decides.
    d = {1: det("Liked"), 10: det("Film"), 20: det("Series")}
    meta = {10: votes(8.5, 9000, 28, 878, 53), 20: votes(7.0, 9000, 10759, 10765)}
    src = FakeSource(
        d, kinds={20: "tv"}, search_map={"x": [1]}, row_genres={1: [28, 878]}, discover_list=[20, 10], row_meta=meta
    )
    out = await run(src, like=["x"], limit=2, genre_aliases=BRIDGE_ALIASES, ranking=Ranking())
    assert [c.tmdb_id for c in out] == [10, 20]


async def test_documentary_dropped_unless_liked_or_requested():
    d = {1: det("Liked"), 10: det("Action film"), 11: det("Crime doc")}
    meta = {10: votes(7, 900, 28), 11: votes(8, 900, 99, 80)}
    src = FakeSource(d, search_map={"x": [1]}, similar_map={1: [10, 11]}, row_genres={1: [28]}, row_meta=meta)
    assert [c.tmdb_id for c in await run(src, like=["x"], limit=10, ranking=Ranking())] == [10]
    liked_doc = FakeSource(d, search_map={"x": [1]}, similar_map={1: [10, 11]}, row_genres={1: [99]}, row_meta=meta)
    assert {c.tmdb_id for c in await run(liked_doc, like=["x"], limit=10, ranking=Ranking())} == {10, 11}
    asked = FakeSource(d, discover_list=[10, 11], row_meta=meta)
    got = await run(asked, genres=["documentary"], limit=10, ranking=Ranking())
    assert 11 in {c.tmdb_id for c in got}
    plain = FakeSource(d, discover_list=[10, 11], row_meta=meta)
    assert [c.tmdb_id for c in await run(plain, limit=10, ranking=Ranking())] == [10]


async def test_liked_title_is_the_most_voted_of_the_top_hits():
    # TMDB search for "The Avengers" leads with the 1961 series (159 votes); the 2012 film has 40k
    series, film, next_one = 2473, 24428, 1003
    d = {series: det("The Avengers 1961"), film: det("The Avengers"), 50: det("SpySeries"), 60: det("Hero film")}
    meta = {series: {"vote_count": 159}, film: {"vote_count": 40252}, next_one: {"vote_count": 99999}}
    src = FakeSource(
        d,
        kinds={series: "tv", 50: "tv"},
        search_map={"The Avengers": [series, film, 7, next_one]},
        similar_map={series: [50], film: [60]},
        row_meta=meta,
    )
    out = await run(src, like=["The Avengers"], kind="movie", ranking=Ranking())
    assert [c.tmdb_id for c in out] == [60]  # 4th hit is outside the top 3, however many votes it has
    first = await run(src, like=["The Avengers"], kind="tv", ranking=Ranking(liked_from_top=1))
    assert [c.tmdb_id for c in first] == [50]


async def test_liked_title_named_exactly_beats_a_more_voted_relative():
    # "Toy Story 4" (9k votes) must not resolve to "Toy Story" (18k) just because it is in the top hits:
    # the profile would be wrong and the named title could come back as a pick
    ts4, ts1, ts3 = 301528, 862, 10193
    d = {ts4: det("Toy Story 4"), ts1: det("Toy Story"), ts3: det("Toy Story 3"), 70: det("Other")}
    rows = lambda i, t, v: {"title": t, "original_title": t, "vote_count": v}  # noqa: E731
    meta = {
        ts4: rows(ts4, "Toy Story 4", 9000),
        ts1: rows(ts1, "Toy Story", 18000),
        ts3: rows(ts3, "Toy Story 3", 15000),
    }
    src = FakeSource(
        d, search_map={"toy story 4": [ts1, ts4, ts3]}, similar_map={ts4: [ts1, 70], ts1: [ts4]}, row_meta=meta
    )
    out = await run(src, like=["toy story 4"], ranking=Ranking())
    assert {c.tmdb_id for c in out} == {ts1, 70}  # resolved to Toy Story 4: its similar list, never itself
    # case and punctuation don't matter; no exact match falls back to the most-voted top hit
    meta[ts4]["title"] = "TOY STORY-4!"
    assert {c.tmdb_id for c in await run(src, like=["toy story 4"], ranking=Ranking())} == {ts1, 70}
    src.search_map["ts"] = [ts4, ts1, ts3]
    assert [c.tmdb_id for c in await run(src, like=["ts"], ranking=Ranking())] == [ts4]  # Toy Story's similar


async def test_blocklist_applies_in_every_mode_and_is_kind_qualified():
    d = {5371: det("iCarly", cert="AL"), 2316: det("The Office", cert="AL"), 1: det("Liked", cert="AL")}
    kinds = {5371: "tv", 2316: "tv"}
    block = Ranking(exclude_tmdb=["tv:5371"])

    def src():
        return FakeSource(
            d,
            kinds=kinds,
            search_map={"icarly": [5371], "x": [1]},
            similar_map={1: [5371, 2316]},
            discover_list=[5371, 2316],
        )

    assert [c.tmdb_id for c in await run(src(), ranking=block)] == [2316]  # plain discover
    assert [c.tmdb_id for c in await run(src(), genres=["comedy"], ranking=block)] == [2316]
    assert [c.tmdb_id for c in await run(src(), like=["x"], ranking=block)] == [2316]
    assert await run(src(), query="icarly", ranking=block) == []  # even when named
    kids = await run(src(), kids_mode=True, kids_age=8, ranking=block)
    assert [c.tmdb_id for c in kids] == [2316]
    kids_like = await run(src(), like=["x"], kids_mode=True, kids_age=8, ranking=block)
    assert [c.tmdb_id for c in kids_like] == [2316]
    # a movie with the same id is a different title
    movie_block = Ranking(exclude_tmdb=["movie:5371"])
    assert {c.tmdb_id for c in await run(src(), ranking=movie_block)} == {5371, 2316}


def _card(i, *svcs):
    from lumitv.models import TitleCard

    return TitleCard(
        token=f"t{i}",
        tmdb_id=i,
        kind="movie",
        title=f"T{i}",
        year=2020,
        overview="",
        poster_url=None,
        services=list(svcs),
        certification="12",
        minutes=100,
        total_minutes=None,
        seasons=None,
        seen=False,
    )


def test_spread_brings_in_a_missing_service_in_place_of_the_lowest_ranked_redundant_card():
    from lumitv.catalog import spread_services

    cards = [_card(1, "prime"), _card(2, "prime"), _card(3, "videoland"), _card(4, "prime"), _card(5, "netflix")]
    out = spread_services(cards, limit=3, window=9)
    # Netflix's best title takes the place of the lowest-ranked card whose services the others still cover;
    # the list stays in rank order.
    assert [c.tmdb_id for c in out] == [1, 3, 5]


def test_spread_leaves_the_list_alone_when_every_service_is_covered_or_absent():
    from lumitv.catalog import spread_services

    cards = [_card(1, "prime", "netflix"), _card(2, "videoland"), _card(3, "prime"), _card(4, "prime")]
    assert [c.tmdb_id for c in spread_services(cards, limit=3, window=9)] == [1, 2, 3]
    only_prime = [_card(i, "prime") for i in range(1, 6)]
    assert [c.tmdb_id for c in spread_services(only_prime, limit=3, window=9)] == [1, 2, 3]


def test_spread_ignores_titles_ranked_outside_the_window():
    from lumitv.catalog import spread_services

    cards = [_card(i, "prime") for i in range(1, 6)] + [_card(6, "netflix")]
    assert [c.tmdb_id for c in spread_services(cards, limit=3, window=5)] == [1, 2, 3]
    assert [c.tmdb_id for c in spread_services(cards, limit=3, window=6)] == [1, 2, 6]


def test_spread_never_drops_a_card_that_is_the_only_one_on_its_service():
    from lumitv.catalog import spread_services

    cards = [_card(1, "prime"), _card(2, "videoland"), _card(3, "netflix"), _card(4, "netflix")]
    assert [c.tmdb_id for c in spread_services(cards, limit=2, window=9)] == [1, 2]


async def test_find_titles_spreads_across_services_and_reports_services_with_nothing():
    flats = {i: {"AE": {119}} for i in range(1, 8)}
    flats[8] = {"AE": {8}}
    src = FakeSource({i: det(f"T{i}", flat=f) for i, f in flats.items()})
    stats = LookupStats()
    out = await run(src, limit=3, stats=stats)
    assert 8 in [c.tmdb_id for c in out]
    assert stats.no_titles_on == ["videoland"]  # configured with providers, nothing in the pool; youtube has none


async def test_find_titles_does_not_spread_when_a_service_was_asked_for():
    flats = {i: {"AE": {119, 8}} for i in range(1, 5)}
    flats[5] = {"NL": {72}, "AE": {119}}
    src = FakeSource({i: det(f"T{i}", flat=f) for i, f in flats.items()})
    stats = LookupStats()
    out = await run(src, limit=3, service="prime", stats=stats)
    assert [c.tmdb_id for c in out] == [1, 2, 3]
    assert stats.no_titles_on == []


# ---- "like X" by franchise and shared keywords -------------------------------------------------------------------

from lumitv.catalog import KEYWORD_MAX_TITLES, KEYWORD_RARE_FLOOR, _named_like, keyword_weights  # noqa: E402
from lumitv.config import Ranking as _Ranking  # noqa: E402

KW = {1: "space opera", 2: "starship", 3: "exciting", 4: "friendship", 5: "romulans", 6: "space travel"}
KW_COUNTS = {1: 187, 2: 120, 3: 59, 4: 4493, 5: 4, 6: 306}


class KwSource(FakeSource):
    """Search finds the liked title (id 100, tv) and whatever rows it is given; discover by keyword returns the ids
    tagged with any asked keyword; details carry keywords and genres."""

    def __init__(self, details, search_rows, keyword_titles, **kw):
        super().__init__(details, **kw)
        self.search_rows = search_rows
        self.keyword_titles = keyword_titles  # tmdb id -> keyword ids
        self.keywords_asked = []

    async def search(self, query, kind):
        return self.search_rows

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
        if keywords:
            self.keywords_asked.append(sorted(keywords))
            ids = [i for i, ks in self.keyword_titles.items() if set(ks) & set(keywords)]
            return [{"id": i, "media_type": kind} for i in ids if self.kinds.get(i, "movie") == kind]
        return await super().discover(kind, region, provider_ids, genres, certification_lte, page, any_genre, min_votes)

    async def keyword_count(self, keyword_id):
        return KW_COUNTS[keyword_id]


def kdet(title, kws=(), genres=(10765,), flat=None, **kw):
    return det(title, flat=flat, genre_ids=list(genres), keywords={k: KW[k] for k in kws}, **kw)


def test_keyword_weights_drop_moods_and_common_words_and_cap_rarity():
    w = keyword_weights(KW, KW_COUNTS)
    assert set(w) == {1, 2, 5, 6}  # "exciting" is a mood, "friendship" is on too many titles
    assert w[2] > w[1] > w[6]  # rarer weighs more
    assert w[5] == math.log(1 + KEYWORD_MAX_TITLES / KEYWORD_RARE_FLOOR)  # rarer than the floor counts as the floor
    assert keyword_weights({9: "space"}, {}) == {}  # unknown count: not used


def test_named_like_takes_whole_word_prefixes_and_subtitles():
    assert _named_like("Star Trek", {"name": "Star Trek: Picard"})
    assert _named_like("Star Trek", {"title": "Star Trek Beyond"})
    assert _named_like("Breaking Bad", {"title": "El Camino: A Breaking Bad Movie"})
    assert not _named_like("Star Trek", {"title": "Star Trekking"})
    assert _named_like("Bad", {"title": "Bad Boys"})  # any whole-word prefix; shared keywords decide the bonus
    assert not _named_like("Breaking Bad", {"title": "Breaking Badly"})


async def test_like_ranks_franchise_then_shared_keywords_above_genre_only_matches():
    liked_row = {"id": 100, "media_type": "tv", "name": "Star Trek", "vote_count": 9000}
    franchise_row = {"id": 11, "media_type": "tv", "name": "Star Trek: Picard"}
    details = {
        100: kdet("Star Trek", kws=(1, 2, 3, 5, 6)),
        11: kdet("Star Trek: Picard", kws=(1, 2), seasons=3, total_minutes=1500, season1_minutes=500),
        12: kdet("Space Show", kws=(1, 6), seasons=1, total_minutes=500, season1_minutes=500),
        13: kdet("Genre Only", kws=(3,), seasons=1, total_minutes=500, season1_minutes=500),  # shares only a mood
    }
    src = KwSource(details, [liked_row, franchise_row], {12: [1, 6], 13: [1]}, kinds={i: "tv" for i in details})
    out = await run(src, like=["Star Trek"], limit=3, ranking=_Ranking())
    assert [c.title for c in out] == ["Star Trek: Picard", "Space Show", "Genre Only"]
    assert out[0].matches == ["starship", "space opera"]
    assert out[2].matches == []
    # searched: usable keywords on at least KEYWORD_RARE_FLOOR titles, never moods or the too-rare "romulans"
    assert all(ks == [1, 2, 6] for ks in src.keywords_asked)


async def test_a_title_only_named_like_the_liked_one_gets_no_franchise_bonus():
    liked_row = {"id": 100, "media_type": "tv", "name": "Friends", "vote_count": 9000}
    namesake = {"id": 21, "media_type": "movie", "title": "Friends with Benefits"}
    details = {
        100: kdet("Friends", kws=(1, 2, 6)),
        21: kdet("Friends with Benefits", kws=(6,)),
        22: kdet("Close Match", kws=(1, 2)),
    }
    src = KwSource(details, [liked_row, namesake], {22: [1, 2]}, kinds={100: "tv"})
    out = await run(src, like=["Friends"], limit=2, ranking=_Ranking())
    assert [c.title for c in out] == ["Close Match", "Friends with Benefits"]


async def test_franchise_titles_are_looked_up_beyond_the_pool_cap():
    liked_row = {"id": 100, "media_type": "tv", "name": "Star Trek", "vote_count": 9000}
    rows = [liked_row] + [{"id": 200 + i, "media_type": "tv", "name": f"Star Trek {i}"} for i in range(5)]
    details = {100: kdet("Star Trek", kws=(1, 2))}
    details |= {200 + i: kdet(f"Star Trek {i}", kws=(1, 2), flat={"AE": set()}) for i in range(4)}  # not on ours
    details[204] = kdet("Star Trek 4", kws=(1, 2))
    details |= {300 + i: kdet(f"Other {i}", kws=(6,)) for i in range(6)}
    src = KwSource(details, rows, {300 + i: [6] for i in range(6)}, kinds={i: "tv" for i in details})
    out = await run(src, like=["Star Trek"], limit=3, candidates=3, ranking=_Ranking())
    assert out[0].title == "Star Trek 4"  # fifth in the franchise, past the cap of 3, still found


def test_tone_words_are_moods_too():
    assert keyword_weights({1: "ambivalent", 2: "hostility", 3: "superhero"}, {1: 210, 2: 32, 3: 1912}).keys() == {3}


def test_searched_keywords_are_the_rarest_and_apart_the_most_common():
    from lumitv.catalog import KEYWORDS_COMMON_SEARCHED, KEYWORDS_SEARCHED, searched_keywords

    counts = {k: 30 + 100 * k for k in range(1, 16)}  # 1 is the rarest, 15 the most common
    counts[16] = 5  # too rare to search
    names = {k: f"kw{k}" for k in counts}
    rare, common = searched_keywords(keyword_weights(names, counts), counts)
    assert rare == list(range(1, KEYWORDS_SEARCHED + 1))
    assert common == [15, 14, 13, 12][:KEYWORDS_COMMON_SEARCHED]
    few = {k: counts[k] for k in (1, 2, 3)}
    assert searched_keywords(keyword_weights(names, few), few) == ([1, 2, 3], [])


async def test_common_keyword_finds_come_on_top_of_the_cap_and_need_a_second_shared_keyword():
    # Spider-Man: its rare keywords find little on our services; "superhero" + "based on comic" find The Boys. One
    # shared common keyword alone (Atlas: "artificial intelligence") is not enough to rank among the rest.
    counts = {50 + k: 40 + 10 * k for k in range(1, 9)} | {59: 1900, 60: 1300}  # 51..58 rare, 59/60 common
    names = {k: f"kw{k}" for k in counts}

    class Src(KwSource):
        async def keyword_count(self, keyword_id):
            return counts[keyword_id]

    def d(title, kws, genres=(10765,)):
        return det(title, genre_ids=list(genres), keywords={k: names[k] for k in kws}, seasons=1, total_minutes=500,
                   season1_minutes=500)  # fmt: skip

    liked_row = {"id": 100, "media_type": "tv", "name": "Spider-Man", "vote_count": 9000}
    details = {
        100: d("Spider-Man", list(counts)),
        71: d("Rare Match", [51], genres=(18,)),  # off-genre, so on score alone Atlas would beat it
        72: d("The Boys", [59, 60]),
        73: d("Atlas", [60]),
    }
    src = Src(details, [liked_row], {71: [51], 72: [59, 60], 73: [60]}, kinds={i: "tv" for i in details})
    out = await run(src, like=["Spider-Man"], limit=3, candidates=1, ranking=_Ranking())
    assert {tuple(ks) for ks in src.keywords_asked} == {tuple(range(51, 59)), (59, 60)}  # two searches
    assert [c.title for c in out] == ["The Boys", "Rare Match", "Atlas"]  # The Boys: past the cap of 1
    assert out[0].matches == ["kw60", "kw59"]  # Atlas, with one shared common keyword, comes after the rest


async def test_a_failed_lookup_of_the_liked_title_is_tried_again():
    liked_row = {"id": 100, "media_type": "tv", "name": "Star Trek", "vote_count": 9000}
    details = {100: kdet("Star Trek", kws=(1, 2)), 12: kdet("Space Show", kws=(1, 2)), 13: kdet("Other", kws=(6,))}

    class Flaky(KwSource):
        failed = False

        async def details(self, tmdb_id, kind, exact_minutes=False):
            if tmdb_id == 100 and not self.failed:
                self.failed = True
                raise TmdbUnavailable("timeout")
            return await super().details(tmdb_id, kind, exact_minutes)

    src = Flaky(details, [liked_row], {12: [1], 13: [6]}, kinds={i: "tv" for i in details})
    stats = LookupStats()
    out = await run(src, like=["Star Trek"], limit=2, ranking=_Ranking(), stats=stats)
    assert out[0].title == "Space Show" and out[0].matches  # keywords were used after the retry
    assert stats.inputs_failed == 1


async def test_thriller_and_horror_fall_back_to_mystery_for_series():
    import json as _json
    from pathlib import Path as _Path

    aliases = _json.loads(
        (_Path(__file__).parent.parent / "content" / "genre_aliases.en.json").read_text(encoding="utf-8")
    )
    src = FakeSource({7: det("Tv")}, kinds={7: "tv"}, discover_list=[7])
    src.genre_lists["tv"]["Mystery"] = 9648
    src.genre_lists["movie"]["Mystery"] = 9648
    for name in ("thriller", "horror", "scary"):
        src.discover_genres.clear()
        stats = LookupStats()
        await run(src, genres=[name], stats=stats, genre_aliases=aliases)
        tv = [g for k, g in src.discover_genres if k == "tv"]
        assert tv and all(g == [9648] for g in tv), name  # series have no Thriller/Horror genre
        assert "tv" not in stats.skipped_kinds


async def test_series_asked_for_as_sci_fi_must_carry_a_sci_fi_keyword_films_unchanged():
    class Src(KwSource):
        async def keyword_ids(self, names):
            return {"space": 1, "alien": 6}

    d = {7: det("Space Show"), 8: det("Film")}
    src = Src(d, [], {7: [1]}, kinds={7: "tv"})
    src.genre_lists["tv"]["Sci-Fi & Fantasy"] = 10765
    out = await run(
        src,
        genres=["sci-fi"],
        genre_aliases={"sci-fi": ["Science Fiction", "Sci-Fi & Fantasy"]},
        genre_keywords={"sci-fi": ["space", "alien"]},
    )
    assert src.keywords_asked and all(ks == [1, 6] for ks in src.keywords_asked)  # only the series discovers
    assert {c.title for c in out} == {"Space Show", "Film"}


async def test_series_genre_keywords_that_fail_to_resolve_fall_back_to_the_plain_genre():
    class Src(KwSource):
        async def keyword_ids(self, names):
            raise TmdbUnavailable("down")

    src = Src({7: det("Show")}, [], {}, kinds={7: "tv"})
    stats = LookupStats()
    out = await run(src, genres=["comedy"], kind="tv", genre_keywords={"comedy": ["sitcom"]}, stats=stats)
    assert [c.title for c in out] == ["Show"] and src.keywords_asked == [] and stats.inputs_failed == 1


def test_shipped_genre_keywords_cover_the_sci_fi_spellings():
    from pathlib import Path as _Path

    from lumitv.catalog import load_genre_keywords

    kw = load_genre_keywords(_Path(__file__).parent.parent / "content", "en")
    assert kw["sci-fi"] == kw["scifi"] == kw["science fiction"] and "space" in kw["sci-fi"]
    assert "_comment" not in kw and "fantasy" in kw
