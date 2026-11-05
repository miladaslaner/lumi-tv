import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from lumitv.catalog_source import TmdbUnavailable
from lumitv.config import Ratings
from lumitv.kids import KidsPolicy, allowed
from lumitv.tmdb import TTL_S, Cache, Tmdb

FIX = Path(__file__).parent / "fixtures" / "tmdb"
RATINGS = Ratings(country="NL", fallback_country="US", fallback_map={"PG-13": "12", "R": "16"})
POLICY = KidsPolicy(
    order=["AL", "6", "9", "12", "14", "16", "18"], toddler_below_age=6, toddler_allowed=["AL"], no_age_max="6"
)


def fx(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def cache(tmp_path):
    return Cache(tmp_path / "lumitv.db")


@pytest.fixture
async def tmdb(cache):
    t = Tmdb("tok", cache, RATINGS, 3.0)
    yield t
    await t.aclose()


@respx.mock
async def test_details_movie_reads_nl_certification_and_flatrate(tmdb):
    route = respx.get("https://api.themoviedb.org/3/movie/100").respond(json=fx("movie_nl"))
    d = await tmdb.details(100, "movie")
    assert d.certification == "16"  # NL says 12, the US R maps to 16: the stricter of the two
    assert d.flatrate_by_region["AE"] == {8, 119}
    assert "NL" not in d.flatrate_by_region  # only rent there
    assert d.poster_url == "https://image.tmdb.org/t/p/w342/p.jpg"
    assert d.runtime_minutes == 120 and d.year == 2019
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"
    assert "watch%2Fproviders%2Crelease_dates" in str(route.calls.last.request.url)


@respx.mock
async def test_certification_falls_back_to_us(tmdb):
    respx.get("https://api.themoviedb.org/3/movie/101").respond(json=fx("movie_us"))
    respx.get("https://api.themoviedb.org/3/movie/102").respond(json=fx("movie_none"))
    assert (await tmdb.details(101, "movie")).certification == "12"
    assert (await tmdb.details(102, "movie")).certification is None


def _release_dates(**by_country: list[tuple[str, int]]) -> dict:
    """{"NL": [("AL", 3), ("12", 5)]} -> TMDB's release_dates shape (type 3 cinema, 4 digital, 5 DVD, 6 TV)."""
    return {"results": [
        {"iso_3166_1": c, "release_dates": [{"certification": cert, "type": t} for cert, t in entries]}
        for c, entries in by_country.items()
    ]}  # fmt: skip


@respx.mock
async def test_nl_al_everywhere_but_us_r_resolves_to_16(tmdb):
    # Love Actually: NL AL in cinemas, on DVD, Blu-ray and TV; US R. One country's AL must not make it a toddler pick.
    love = fx("movie_nl") | {
        "release_dates": _release_dates(NL=[("AL", 3), ("AL", 5), ("AL", 5), ("AL", 6)], US=[("R", 3)], GB=[("15", 3)])
    }
    respx.get("https://api.themoviedb.org/3/movie/508").respond(json=love)
    d = await tmdb.details(508, "movie")
    assert d.certification == "16"
    for age in (None, 3, 5, 10, 15, 99):
        assert not allowed(d.certification, age, True, POLICY), age


@respx.mock
async def test_strictest_nl_entry_wins_over_the_cinema_one(tmdb):
    # La La Land: NL AL in cinemas but 12 on DVD; US PG-13 (12 too). The DVD rating is the one that holds.
    lala = fx("movie_nl") | {"release_dates": _release_dates(NL=[("AL", 3), ("12", 5)], US=[("PG-13", 3)])}
    respx.get("https://api.themoviedb.org/3/movie/313369").respond(json=lala)
    assert (await tmdb.details(313369, "movie")).certification == "12"


@respx.mock
async def test_unknown_labels_never_make_a_title_eligible(tmdb):
    # a label with no age ("NR") is kept only when nothing with an age is there, and kids mode then excludes it
    nr = fx("movie_nl") | {"release_dates": _release_dates(NL=[("NR", 3)])}
    respx.get("https://api.themoviedb.org/3/movie/110").respond(json=nr)
    d = await tmdb.details(110, "movie")
    assert d.certification == "NR" and not allowed("NR", 15, True, POLICY)
    # with a known rating next to it, the unknown one is ignored for strictness
    mixed = fx("movie_nl") | {"release_dates": _release_dates(NL=[("NR", 3), ("6", 5)])}
    respx.get("https://api.themoviedb.org/3/movie/111").respond(json=mixed)
    assert (await tmdb.details(111, "movie")).certification == "6"
    # an unmapped fallback label alone is nothing
    unmapped = fx("movie_nl") | {"release_dates": _release_dates(US=[("NC-17", 3)])}
    respx.get("https://api.themoviedb.org/3/movie/112").respond(json=unmapped)
    assert (await tmdb.details(112, "movie")).certification is None


@respx.mock
async def test_series_take_the_strictest_rating_of_the_home_country_too(cache):
    t = Tmdb("tok", cache, RATINGS, 3.0, ages={"AL": 0, "6": 6, "12": 12, "14": 14, "16": 16})
    series = fx("tv_series") | {"content_ratings": {"results": [{"iso_3166_1": "NL", "rating": "6"}, {"iso_3166_1": "NL", "rating": "12"},
                                                               {"iso_3166_1": "US", "rating": "TV-14"}]}}  # fmt: skip
    respx.get("https://api.themoviedb.org/3/tv/201").respond(json=series)
    respx.get("https://api.themoviedb.org/3/tv/201/season/1").respond(json=fx("tv_season1"))
    respx.get("https://api.themoviedb.org/3/tv/201/season/2").respond(json=fx("tv_season1"))
    assert (await t.details(201, "tv")).certification == "12"  # TV-14 is not in this fallback map: ignored
    await t.aclose()


@respx.mock
async def test_details_tv_minutes(tmdb):
    respx.get("https://api.themoviedb.org/3/tv/200").respond(json=fx("tv_series"))
    respx.get("https://api.themoviedb.org/3/tv/200/season/1").respond(json=fx("tv_season1"))
    respx.get("https://api.themoviedb.org/3/tv/200/season/2").respond(json=fx("tv_season1"))
    d = await tmdb.details(200, "tv")
    assert (d.season1_minutes, d.total_minutes, d.seasons) == (160, 533, 2)
    assert d.certification == "14"
    assert d.flatrate_by_region == {"NL": {72}}


@respx.mock
async def test_details_missing_poster_and_runtime(tmdb):
    respx.get("https://api.themoviedb.org/3/movie/103").respond(json=fx("movie_bare"))
    d = await tmdb.details(103, "movie")
    assert d.poster_url is None and d.runtime_minutes is None


@respx.mock
async def test_cache_hit_makes_no_second_request(cache):
    events = []
    t = Tmdb("tok", cache, RATINGS, 3.0, on_cache=events.append)
    route = respx.get("https://api.themoviedb.org/3/movie/100").respond(json=fx("movie_nl"))
    await t.details(100, "movie")
    await t.details(100, "movie")
    assert route.call_count == 1
    assert events == [False, True]
    await t.aclose()


@respx.mock
async def test_serves_stale_cache_when_tmdb_fails(cache, tmdb):
    route = respx.get("https://api.themoviedb.org/3/movie/100").respond(json=fx("movie_nl"))
    await tmdb.details(100, "movie")
    key = str(route.calls.last.request.url)
    body, _ = cache.get(key)
    cache.put(key, body, fetched_at=time.time() - TTL_S - 60)
    route.respond(500)
    d = await tmdb.details(100, "movie")
    assert d.title == "Movie NL"
    assert route.call_count == 2  # stale entry was refetched first


@respx.mock
async def test_timeout_is_3s(tmdb):
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    respx.get("https://api.themoviedb.org/3/movie/100").mock(side_effect=slow)
    start = time.monotonic()
    with pytest.raises(TmdbUnavailable):
        await tmdb.details(100, "movie")
    assert time.monotonic() - start < 4


@respx.mock
async def test_discover_params_and_similar_dedupe(tmdb):
    route = respx.get("https://api.themoviedb.org/3/discover/movie").respond(json={"results": [{"id": 1}]})
    out = await tmdb.discover("movie", "AE", {8, 119}, [16], "12")
    assert out == [{"id": 1, "media_type": "movie"}]
    q = route.calls.last.request.url.params
    assert q["with_watch_providers"] == "8|119" and q["watch_region"] == "AE"
    assert q["with_watch_monetization_types"] == "flatrate"
    assert q["certification_country"] == "NL" and q["certification.lte"] == "12"
    assert q["include_adult"] == "false"
    assert "vote_count.gte" not in q  # no floor asked: none sent (page-1 URLs are cache keys shared with warming)
    await tmdb.discover("movie", "AE", {8, 119}, [16], "12", min_votes=300)
    assert route.calls.last.request.url.params["vote_count.gte"] == "300"
    respx.get("https://api.themoviedb.org/3/movie/5/recommendations").respond(json={"results": [{"id": 1}, {"id": 2}]})
    respx.get("https://api.themoviedb.org/3/movie/5/similar").respond(json={"results": [{"id": 2}, {"id": 3}]})
    assert [r["id"] for r in await tmdb.similar(5, "movie")] == [1, 2, 3]


@respx.mock
async def test_a_429_waits_retry_after_and_is_tried_once_more(tmdb):
    route = respx.get("https://api.themoviedb.org/3/movie/100")
    route.side_effect = [httpx.Response(429, headers={"retry-after": "0"}), httpx.Response(200, json=fx("movie_nl"))]
    assert (await tmdb.details(100, "movie")).title == "Movie NL"
    assert route.call_count == 2 and tmdb.rate_limited == 1
    # a second 429 is a failure like any other: stale cache if there is one, else unavailable
    again = respx.get("https://api.themoviedb.org/3/movie/101").respond(429, headers={"retry-after": "0"})
    with pytest.raises(TmdbUnavailable):
        await tmdb.details(101, "movie")
    assert again.call_count == 2 and tmdb.rate_limited == 3


def test_retry_after_is_honoured_up_to_the_cap():
    from lumitv.tmdb import RETRY_AFTER_DEFAULT_S, RETRY_AFTER_MAX_S, _retry_wait

    assert _retry_wait(httpx.Response(429, headers={"retry-after": "2"})) == 2.0
    assert _retry_wait(httpx.Response(429, headers={"retry-after": "99"})) == RETRY_AFTER_MAX_S
    assert _retry_wait(httpx.Response(429)) == RETRY_AFTER_DEFAULT_S
    assert (
        _retry_wait(httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}))
        == RETRY_AFTER_DEFAULT_S
    )
    assert _retry_wait(httpx.Response(429, headers={"retry-after": "-3"})) == 0.0


@respx.mock
async def test_requests_in_flight_never_exceed_the_concurrency_bound(cache):
    t = Tmdb("tok", cache, RATINGS, 3.0, concurrency=3)
    inflight = {"now": 0, "max": 0}

    async def slow(request):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        await asyncio.sleep(0.01)
        inflight["now"] -= 1
        return httpx.Response(200, json={"results": []})

    respx.get(url__regex=r".*/discover/tv.*").mock(side_effect=slow)
    await asyncio.gather(*(t.discover("tv", "AE", {8}, None, None, page=p) for p in range(1, 13)))  # 12 distinct URLs
    assert inflight["max"] == 3
    await t.aclose()


@respx.mock
async def test_adult_titles_are_asked_out_and_dropped_everywhere(tmdb):
    adult = {"id": 9, "adult": True, "title": "X"}
    disc = respx.get("https://api.themoviedb.org/3/discover/tv").respond(json={"results": [{"id": 1}, adult]})
    assert [r["id"] for r in await tmdb.discover("tv", "AE", {8}, None, None)] == [1]
    multi = respx.get("https://api.themoviedb.org/3/search/multi").respond(
        json={"results": [{"id": 1, "media_type": "movie"}, {**adult, "media_type": "movie"}]}
    )
    assert [r["id"] for r in await tmdb.search("x", None)] == [1]
    assert multi.calls.last.request.url.params["include_adult"] == "false"
    one = respx.get("https://api.themoviedb.org/3/search/movie").respond(json={"results": [adult, {"id": 2}]})
    assert [r["id"] for r in await tmdb.search("x", "movie")] == [2]
    assert one.calls.last.request.url.params["include_adult"] == "false"
    assert disc.calls.last.request.url.params["include_adult"] == "false"
    # similar/recommendations have no switch: the rows' flag is what filters
    respx.get("https://api.themoviedb.org/3/movie/5/recommendations").respond(json={"results": [adult, {"id": 2}]})
    respx.get("https://api.themoviedb.org/3/movie/5/similar").respond(json={"results": [{"id": 3, "adult": False}]})
    assert [r["id"] for r in await tmdb.similar(5, "movie")] == [2, 3]
    respx.get("https://api.themoviedb.org/3/movie/100").respond(json=fx("movie_nl") | {"adult": True})
    assert (await tmdb.details(100, "movie")).adult is True
    respx.get("https://api.themoviedb.org/3/movie/101").respond(json=fx("movie_us"))
    assert (await tmdb.details(101, "movie")).adult is False


@respx.mock
async def test_stale_fallback_reports_and_discover_page_param(cache):
    stale = []
    t = Tmdb("tok", cache, RATINGS, 3.0, on_stale=lambda path, err: stale.append((path, err)))
    route = respx.get("https://api.themoviedb.org/3/movie/100").respond(json=fx("movie_nl"))
    await t.details(100, "movie")
    key = str(route.calls.last.request.url)
    cache.put(key, cache.get(key)[0], fetched_at=time.time() - TTL_S - 60)
    route.respond(401)
    await t.details(100, "movie")
    assert len(stale) == 1 and stale[0][0] == "/movie/100" and "401" in stale[0][1]

    d = respx.get("https://api.themoviedb.org/3/discover/tv").respond(json={"results": []})
    await t.discover("tv", "AE", {8}, None, None)
    await t.discover("tv", "AE", {8}, None, None, page=3)
    assert "page" not in d.calls[0].request.url.params and d.calls[1].request.url.params["page"] == "3"
    await t.aclose()


def _series_with_runtime():
    return {**fx("tv_series"), "episode_run_time": [40, 50], "number_of_episodes": 20,
            "seasons": [{"season_number": 0, "episode_count": 2}, {"season_number": 1, "episode_count": 8},
                                                                           {"season_number": 2, "episode_count": 12}]}  # fmt: skip


@respx.mock
async def test_tv_minutes_estimated_without_season_fetch(tmdb):
    respx.get("https://api.themoviedb.org/3/tv/200").respond(json=_series_with_runtime())
    s1 = respx.get("https://api.themoviedb.org/3/tv/200/season/1").respond(json=fx("tv_season1"))
    d = await tmdb.details(200, "tv")
    assert s1.call_count == 0
    assert (d.season1_minutes, d.total_minutes) == (360, 900)  # avg 45 x 8 and x 20
    assert d.season2_minutes == 540  # avg 45 x 12


@respx.mock
async def test_tv_minutes_exact_fetches_seasons_one_and_two(tmdb):
    respx.get("https://api.themoviedb.org/3/tv/200").respond(json=_series_with_runtime())
    s1 = respx.get("https://api.themoviedb.org/3/tv/200/season/1").respond(json=fx("tv_season1"))
    s2 = respx.get("https://api.themoviedb.org/3/tv/200/season/2").respond(
        json={"episodes": [{"runtime": 30}, {"runtime": 40}]}
    )
    d = await tmdb.details(200, "tv", exact_minutes=True)
    assert (s1.call_count, s2.call_count) == (1, 1)
    assert (d.season1_minutes, d.season2_minutes) == (160, 70)


@respx.mock
async def test_tv_exact_season_two_failure_keeps_season_one(tmdb):
    respx.get("https://api.themoviedb.org/3/tv/200").respond(json=_series_with_runtime())
    respx.get("https://api.themoviedb.org/3/tv/200/season/1").respond(json=fx("tv_season1"))
    respx.get("https://api.themoviedb.org/3/tv/200/season/2").respond(404)
    d = await tmdb.details(200, "tv", exact_minutes=True)
    assert d.season1_minutes == 160 and d.season2_minutes is None


@respx.mock
async def test_tv_minutes_exact_fetches_season_one(tmdb):
    respx.get("https://api.themoviedb.org/3/tv/200").respond(json=_series_with_runtime())
    s1 = respx.get("https://api.themoviedb.org/3/tv/200/season/1").respond(json=fx("tv_season1"))
    respx.get("https://api.themoviedb.org/3/tv/200/season/2").respond(json=fx("tv_season1"))
    d = await tmdb.details(200, "tv", exact_minutes=True)
    assert s1.call_count == 1 and d.season1_minutes == 160


@respx.mock
async def test_genres_name_to_id_per_kind_and_cached(tmdb):
    movie = respx.get("https://api.themoviedb.org/3/genre/movie/list").respond(
        json={"genres": [{"id": 878, "name": "Science Fiction"}, {"id": 35, "name": "Comedy"}]}
    )
    tv = respx.get("https://api.themoviedb.org/3/genre/tv/list").respond(
        json={"genres": [{"id": 10765, "name": "Sci-Fi & Fantasy"}, {"id": 10762, "name": "Kids"}]}
    )
    assert await tmdb.genres("movie") == {"Science Fiction": 878, "Comedy": 35}
    assert await tmdb.genres("tv") == {"Sci-Fi & Fantasy": 10765, "Kids": 10762}
    assert "language=en" in str(movie.calls.last.request.url)
    await tmdb.genres("movie")
    assert movie.call_count == 1 and tv.call_count == 1  # second read from cache


@respx.mock
async def test_discover_any_genre_or_joins(tmdb):
    route = respx.get("https://api.themoviedb.org/3/discover/tv").respond(json={"results": []})
    await tmdb.discover("tv", "AE", {8}, [10765, 10759], None, any_genre=True)
    assert route.calls.last.request.url.params["with_genres"] == "10765|10759"
    await tmdb.discover("tv", "AE", {8}, [10765, 10759], None)
    assert route.calls.last.request.url.params["with_genres"] == "10765,10759"


def test_cache_read_never_waits_for_a_write_in_progress(tmp_path):
    """WAL: a writer holding its lock (the warm job) must not block a read on the event loop (19.5 s stalls, 2026-11-04)."""
    import sqlite3

    c = Cache(tmp_path / "lumitv.db")
    c.put("u1", "body")
    other = sqlite3.connect(tmp_path / "lumitv.db", isolation_level=None)
    other.execute("BEGIN EXCLUSIVE")  # in WAL mode the same as IMMEDIATE: readers go on
    other.execute("INSERT OR REPLACE INTO http_cache VALUES ('u2', 'x', 0)")  # uncommitted, lock held
    t = time.monotonic()
    assert c.get("u1")[0] == "body" and c.get("u2") is None
    assert time.monotonic() - t < 0.5
    other.execute("ROLLBACK")
    other.close()
    c.close()


@respx.mock
async def test_details_read_genres_and_keywords_of_films_and_series(tmdb):
    movie = fx("movie_nl") | {
        "genres": [{"id": 878, "name": "Science Fiction"}],
        "keywords": {"keywords": [{"id": 1, "name": "space opera"}]},
    }
    series = fx("tv_series") | {
        "genres": [{"id": 10765, "name": "Sci-Fi & Fantasy"}],
        "keywords": {"results": [{"id": 2, "name": "starship"}]},
    }
    route = respx.get("https://api.themoviedb.org/3/movie/100").respond(json=movie)
    respx.get("https://api.themoviedb.org/3/tv/200").respond(json=series)
    respx.get("https://api.themoviedb.org/3/tv/200/season/1").respond(json=fx("tv_season1"))
    respx.get("https://api.themoviedb.org/3/tv/200/season/2").respond(json=fx("tv_season1"))
    m, s = await tmdb.details(100, "movie"), await tmdb.details(200, "tv")
    assert (m.genre_ids, m.keywords) == ([878], {1: "space opera"})
    assert (s.genre_ids, s.keywords) == ([10765], {2: "starship"})
    assert "keywords" in str(route.calls.last.request.url)


@respx.mock
async def test_keyword_count_adds_films_and_series(tmdb):
    respx.get("https://api.themoviedb.org/3/discover/movie", params={"with_keywords": "7"}).respond(
        json={"total_results": 30}
    )
    respx.get("https://api.themoviedb.org/3/discover/tv", params={"with_keywords": "7"}).respond(
        json={"total_results": 12}
    )
    assert await tmdb.keyword_count(7) == 42


@respx.mock
async def test_discover_by_keywords_ors_them(tmdb):
    route = respx.get("https://api.themoviedb.org/3/discover/tv").respond(json={"results": [{"id": 5}]})
    rows = await tmdb.discover("tv", "AE", {8}, None, None, keywords=[3, 1])
    assert rows == [{"id": 5, "media_type": "tv"}]
    assert route.calls.last.request.url.params["with_keywords"] == "3|1"


@respx.mock
async def test_keyword_ids_take_exact_name_matches_only(tmdb):
    respx.get("https://api.themoviedb.org/3/search/keyword", params={"query": "space"}).respond(
        json={"results": [{"id": 1, "name": "space station"}, {"id": 9882, "name": "Space"}]}
    )
    respx.get("https://api.themoviedb.org/3/search/keyword", params={"query": "zzz"}).respond(json={"results": []})
    assert await tmdb.keyword_ids(["space", "zzz"]) == {"space": 9882}


@respx.mock
async def test_keyword_counts_stay_cached_for_a_month(tmdb, cache):
    m = respx.get("https://api.themoviedb.org/3/discover/movie", params={"with_keywords": "7"}).respond(
        json={"total_results": 3}
    )
    respx.get("https://api.themoviedb.org/3/discover/tv", params={"with_keywords": "7"}).respond(
        json={"total_results": 1}
    )
    assert await tmdb.keyword_count(7) == 4
    with cache._lock:  # age every entry by 2 days: past the default day, well within the keyword month
        cache._db.execute("UPDATE http_cache SET fetched_at = fetched_at - ?", (2 * 86400,))
    assert await tmdb.keyword_count(7) == 4 and m.call_count == 1
