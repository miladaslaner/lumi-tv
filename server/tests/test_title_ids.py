import asyncio
import re
import time

import httpx
import pytest
import respx

from lumitv.catalog import Pick
from lumitv.config import LaunchCfg
from lumitv.services import Service, google_tv_url
from lumitv.title_ids import API_URL, WIKIDATA_MISS_TTL_S, WIKIDATA_TTL_S, TitleIdResolver, Wikidata, extract_ids
from lumitv.tmdb import Cache, Tmdb
from test_tmdb import RATINGS

UPLOAD = "/g/11f7dr1t_l"


def claim(value, rank="normal"):
    return {"rank": rank, "mainsnak": {"datavalue": {"value": value}}}


def entity(qid="Q1", **claims):
    return {"entities": {qid: {"id": qid, "claims": claims}}}


# ---- id extraction ----


def test_p2671_is_used():
    assert extract_ids(entity(P2671=[claim(UPLOAD)], P646=[claim("/m/0abc")])) == {"google_id": UPLOAD}


def test_p646_when_p2671_absent_and_netflix_from_p1874():
    d = entity(P646=[claim("/m/0gg70vv")], P1874=[claim("70195800")])
    assert extract_ids(d) == {"google_id": "/m/0gg70vv", "netflix_id": "70195800"}


def test_no_id_properties_is_empty():
    assert extract_ids(entity(P31=[claim({"id": "Q5398426"})])) == {}
    assert extract_ids({}) == {}


def test_preferred_rank_first_and_deprecated_skipped():
    d = entity(P2671=[claim("/g/old", rank="deprecated"), claim("/g/normal"), claim("/g/best", rank="preferred")])
    assert extract_ids(d) == {"google_id": "/g/best"}
    assert extract_ids(entity(P2671=[claim("/g/old", rank="deprecated")])) == {}


def test_malformed_values_ignored_and_redirected_entity_read():
    d = entity("Q999", P2671=[claim("not an id"), claim({"x": 1})], P646=[claim("/m/0x1")], P1874=[claim("80/../1")])
    assert extract_ids(d) == {"google_id": "/m/0x1"}  # the entity key is the redirect target, not the asked id


def test_url_encodes_the_whole_id():
    assert (
        google_tv_url("https://tv.google.com/asset/{google_id_url}", UPLOAD)
        == "https://tv.google.com/asset/%2Fg%2F11f7dr1t_l"
    )


# ---- Wikidata client ----


@pytest.fixture
def cache(tmp_path):
    return Cache(tmp_path / "c.db")


@pytest.fixture
async def wikidata(cache):
    w = Wikidata(cache, timeout_s=0.5)
    yield w
    await w.aclose()


def api(entities_by_qid, calls=None):
    """A respx side effect answering wbgetentities with the asked items present in `entities_by_qid`."""

    def answer(request):
        asked = request.url.params["ids"].split("|")
        if calls is not None:
            calls.append(asked)
        found = {q: entities_by_qid[q]["entities"][q] for q in asked if q in entities_by_qid}
        found |= {q: {"id": q, "missing": ""} for q in asked if q not in entities_by_qid}
        return httpx.Response(200, json={"entities": found})

    return answer


@respx.mock
async def test_wikidata_fetches_once_then_cache_with_generic_user_agent(wikidata):
    route = respx.get(API_URL).mock(side_effect=api({"Q56275161": entity("Q56275161", P2671=[claim(UPLOAD)])}))
    assert await wikidata.ids("Q56275161") == {"google_id": UPLOAD}
    assert await wikidata.ids("Q56275161") == {"google_id": UPLOAD}
    assert route.call_count == 1
    req = route.calls.last.request
    assert req.url.params["action"] == "wbgetentities" and req.url.params["maxlag"] == "5"
    ua = req.headers["user-agent"]
    # Wikimedia's robot policy form, minus the contact nobody configured (Wikidata then refuses: doctor says so)
    assert re.fullmatch(r"lumi-tv/\S+ python-httpx/\S+", ua), ua


@respx.mock
async def test_wikidata_user_agent_carries_the_configured_contact(cache):
    route = respx.get(API_URL).mock(side_effect=api({}))
    w = Wikidata(cache, timeout_s=0.5, contact="https://example.org/me\nx")
    try:
        await w.ids("Q9")
    finally:
        await w.aclose()
    ua = route.calls.last.request.headers["user-agent"]
    assert re.fullmatch(r"lumi-tv/\S+ \(https://example\.org/me x\) python-httpx/\S+", ua), ua


@respx.mock
async def test_wikidata_batches_a_burst_into_few_serial_requests(wikidata):
    """The warm job asks for hundreds of items at once: 120 items -> 3 requests of <= 50, never two at a time."""
    calls, live, peak = [], 0, 0
    answer = api({f"Q{i}": entity(f"Q{i}", P1874=[claim(str(i))]) for i in range(1, 121)}, calls)

    async def serial(request):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        return answer(request)

    respx.get(API_URL).mock(side_effect=serial)
    got = await asyncio.gather(*(wikidata.ids(f"Q{i}") for i in range(1, 121)))
    assert got == [{"netflix_id": str(i)} for i in range(1, 121)]
    assert [len(c) for c in calls] == [50, 50, 20] and peak == 1


@respx.mock
async def test_wikidata_newest_lookup_goes_first(cache):
    """A pick being shown must not wait behind the warm job's queue."""
    calls = []
    respx.get(API_URL).mock(side_effect=api({}, calls))
    w = Wikidata(cache, timeout_s=2)
    try:
        warm = [asyncio.ensure_future(w.ids(f"Q{i}")) for i in range(1, 101)]
        await asyncio.sleep(0)
        await w.ids("Q999")
        await asyncio.gather(*warm)
    finally:
        await w.aclose()
    assert "Q999" in calls[0]


@respx.mock
async def test_wikidata_miss_is_cached(wikidata):
    route = respx.get(API_URL).mock(side_effect=api({"Q2": entity("Q2")}))
    assert await wikidata.ids("Q2") == {} and await wikidata.ids("Q2") == {}
    assert await wikidata.ids("Q3") == {} and await wikidata.ids("Q3") == {}  # "missing" item
    assert route.call_count == 2


@respx.mock
async def test_wikidata_rejected_id_is_a_miss_and_the_rest_still_resolve(wikidata):
    calls = []
    ok = api({"Q5": entity("Q5", P1874=[claim("5")])}, calls)

    def answer(request):
        if "Q999999999999" in request.url.params["ids"].split("|"):
            calls.append(request.url.params["ids"].split("|"))
            return httpx.Response(200, json={"error": {"code": "no-such-entity", "id": "Q999999999999"}})
        return ok(request)

    respx.get(API_URL).mock(side_effect=answer)
    a, b = await asyncio.gather(wikidata.ids("Q5"), wikidata.ids("Q999999999999"))
    assert (a, b) == ({"netflix_id": "5"}, {})
    assert await wikidata.ids("Q999999999999") == {} and len(calls) == 2  # cached as a miss


@respx.mock
async def test_wikidata_merged_item_is_read_from_its_target(wikidata):
    target = {"id": "Q20", "redirects": {"from": "Q10", "to": "Q20"}, "claims": {"P1874": [claim("20")]}}
    respx.get(API_URL).respond(json={"entities": {"Q20": target}})
    assert await wikidata.ids("Q10") == {"netflix_id": "20"}


@respx.mock
async def test_wikidata_miss_expires_after_the_long_ttl(wikidata, cache):
    route = respx.get(API_URL).mock(side_effect=api({"Q2": entity("Q2", P646=[claim("/m/0new")])}))
    cache.put("wikidata:Q2:ids", "{}", fetched_at=time.time() - WIKIDATA_MISS_TTL_S - 1)
    assert await wikidata.ids("Q2") == {"google_id": "/m/0new"} and route.call_count == 1


@respx.mock
async def test_wikidata_miss_expires_sooner_than_a_hit(wikidata, cache):
    assert WIKIDATA_MISS_TTL_S < WIKIDATA_TTL_S
    age = time.time() - WIKIDATA_MISS_TTL_S - 1  # older than a miss may be, younger than a hit may be
    route = respx.get(API_URL).mock(side_effect=api({"Q7": entity("Q7", P646=[claim("/m/0new")])}))
    cache.put("wikidata:Q8:ids", '{"netflix_id": "5"}', fetched_at=age)
    assert await wikidata.ids("Q8") == {"netflix_id": "5"} and route.call_count == 0  # hit still fresh
    cache.put("wikidata:Q7:ids", "{}", fetched_at=age)
    assert await wikidata.ids("Q7") == {"google_id": "/m/0new"} and route.call_count == 1  # miss refetched


@respx.mock
async def test_wikidata_serves_stale_ids_when_the_refresh_fails(wikidata, cache):
    respx.get(API_URL).mock(side_effect=httpx.ConnectError("down"))
    cache.put("wikidata:Q6:ids", '{"netflix_id": "1"}', fetched_at=time.time() - WIKIDATA_TTL_S - 1)
    assert await wikidata.ids("Q6") == {"netflix_id": "1"}


@respx.mock
async def test_wikidata_errors_and_timeouts_are_none_and_not_cached(wikidata):
    route = respx.get(API_URL).mock(side_effect=[httpx.ConnectError("down"), httpx.Response(503)])
    assert await wikidata.ids("Q4") == {}
    assert await wikidata.ids("Q4") == {}
    assert route.call_count == 2

    async def slow(request):
        await asyncio.sleep(2)
        return api({"Q5": entity("Q5", P2671=[claim(UPLOAD)])})(request)

    respx.get(API_URL).mock(side_effect=slow)
    t = time.monotonic()
    assert await wikidata.ids("Q5") == {}
    assert time.monotonic() - t < 1.5


@pytest.mark.parametrize("refusal", [
    httpx.Response(403, text="Please respect our robot policy"),
    httpx.Response(429, headers={"retry-after": "5"}),
    httpx.Response(200, json={"error": {"code": "maxlag", "info": "Waiting for a server: 6 seconds lagged"}}),
])  # fmt: skip
@respx.mock
async def test_wikidata_refusal_pauses_requests_and_keeps_stale_ids(wikidata, cache, refusal):
    route = respx.get(API_URL).mock(return_value=refusal)
    cache.put("wikidata:Q6:ids", '{"netflix_id": "1"}', fetched_at=time.time() - WIKIDATA_TTL_S - 1)
    assert await wikidata.ids("Q6") == {"netflix_id": "1"}
    assert await wikidata.ids("Q7") == {} and await wikidata.ids("Q6") == {"netflix_id": "1"}
    assert route.call_count == 1  # nothing more is sent during the cool-down


async def test_wikidata_rejects_non_item_ids(wikidata):
    assert await wikidata.ids("../x") == {} and await wikidata.ids("") == {}


@respx.mock
async def test_tmdb_external_ids_cached(cache):
    route = respx.get("https://api.themoviedb.org/3/tv/37680/external_ids").respond(
        json={"freebase_mid": "/m/0gg70vv", "wikidata_id": "Q1"}
    )
    t = Tmdb("tok", cache, RATINGS, 3.0)
    try:
        assert (await t.external_ids("tv", 37680))["freebase_mid"] == "/m/0gg70vv"
        await t.external_ids("tv", 37680)
    finally:
        await t.aclose()
    assert route.call_count == 1


# ---- resolution order and picks ----


class FakeTmdb:
    def __init__(self, ext, delay=0.0):
        self.ext, self.delay, self.calls = ext, delay, []

    async def external_ids(self, kind, tmdb_id):
        self.calls.append((kind, tmdb_id))
        await asyncio.sleep(self.delay)
        v = self.ext[(kind, tmdb_id)]
        if isinstance(v, Exception):
            raise v
        return v


class FakeWikidata:
    def __init__(self, ids):
        self.map, self.calls = ids, []

    async def ids(self, qid):
        self.calls.append(qid)
        return self.map.get(qid, {})


SERVICES = {
    "prime": Service("prime", "Prime Video", frozenset({119}), "AE", "com.prime", "any", google_tv=True),
    "netflix": Service(
        "netflix",
        "Netflix",
        frozenset({8}),
        "AE",
        "com.netflix.ninja",
        "off",
        LaunchCfg(action="VIEW", data="https://www.netflix.com/title/{netflix_id}"),
    ),
    "videoland": Service("videoland", "Videoland", frozenset({72}), "NL", "nl.videoland", "on"),
}
G, N = frozenset({"google_id"}), frozenset({"netflix_id"})


def pick(token, tmdb_id, kind="tv", service="prime"):
    return Pick(
        token=token,
        tmdb_id=tmdb_id,
        kind=kind,
        title="T",
        year=2020,
        overview="",
        poster_url=None,
        services=[service],
        certification="12",
        minutes=None,
        total_minutes=None,
        seasons=None,
        seen=False,
        kids_mode=False,
        kids_age=None,
        seq=1,
    )


def test_services_want_the_ids_their_launch_uses():
    assert SERVICES["prime"].wanted_ids == G and SERVICES["netflix"].wanted_ids == N
    assert SERVICES["videoland"].wanted_ids == frozenset()


async def test_google_order_freebase_mid_then_wikidata():
    tmdb = FakeTmdb({
        ("tv", 1): {"freebase_mid": "/m/0gg70vv", "wikidata_id": "Q1"},
        ("tv", 2): {"freebase_mid": None, "wikidata_id": "Q56275161"},
        ("tv", 3): {"freebase_mid": None, "wikidata_id": None},
        ("tv", 4): {"freebase_mid": "garbage", "wikidata_id": "Q4"},
        ("tv", 5): RuntimeError("TMDB down"),
    })  # fmt: skip
    wd = FakeWikidata({"Q1": {"google_id": "/g/unused"}, "Q56275161": {"google_id": UPLOAD, "netflix_id": "1"}})
    g = TitleIdResolver(tmdb, wd, SERVICES, wait_s=1)
    assert await g.resolve("tv", 1, G) == {"google_id": "/m/0gg70vv"} and wd.calls == []  # TMDB's freebase_mid wins
    assert await g.resolve("tv", 2, G) == {"google_id": UPLOAD}  # only the wanted ids
    assert await g.resolve("tv", 3, G) == {}  # no wikidata id: no lookup
    assert await g.resolve("tv", 4, G) == {} and wd.calls == ["Q56275161", "Q4"]
    assert await g.resolve("tv", 5, G) == {}  # never raises
    assert await g.resolve("tv", 1, frozenset()) == {} and tmdb.calls[0] == ("tv", 1)


async def test_netflix_id_comes_from_wikidata_even_when_tmdb_has_the_google_id():
    tmdb = FakeTmdb({("tv", 37680): {"freebase_mid": "/m/0gg70vv", "wikidata_id": "Q370185"}})
    wd = FakeWikidata({"Q370185": {"netflix_id": "70195800"}})
    g = TitleIdResolver(tmdb, wd, SERVICES, wait_s=1)
    assert await g.resolve("tv", 37680, N) == {"netflix_id": "70195800"}
    assert await g.resolve("tv", 37680, G | N) == {"google_id": "/m/0gg70vv", "netflix_id": "70195800"}


async def test_for_picks_only_services_that_want_ids_in_parallel():
    tmdb = FakeTmdb({("tv", i): {"freebase_mid": f"/m/0{i}"} for i in (1, 2, 3)}, delay=0.2)
    g = TitleIdResolver(tmdb, FakeWikidata({}), SERVICES, wait_s=1)
    t = time.monotonic()
    out = await g.for_picks([pick("a", 1), pick("b", 2), pick("v", 3, service="videoland")])
    assert out == {"a": {"google_id": "/m/01"}, "b": {"google_id": "/m/02"}}
    assert ("tv", 3) not in tmdb.calls  # Videoland wants no ids
    assert time.monotonic() - t < 0.35  # resolved concurrently


async def test_for_picks_sends_without_unresolved_ids_after_the_wait():
    tmdb = FakeTmdb({("tv", 1): {"freebase_mid": "/m/0fast"}, ("tv", 2): {"freebase_mid": "/m/0slow"}})

    async def ext(kind, tmdb_id):
        tmdb.calls.append(tmdb_id)
        await asyncio.sleep(0 if tmdb_id == 1 else 0.3)
        return tmdb.ext[(kind, tmdb_id)]

    tmdb.external_ids = ext
    g = TitleIdResolver(tmdb, FakeWikidata({}), SERVICES, wait_s=0.05)
    t = time.monotonic()
    assert await g.for_picks([pick("a", 1), pick("b", 2)]) == {"a": {"google_id": "/m/0fast"}}
    assert time.monotonic() - t < 0.2
    await asyncio.sleep(0.4)  # the slow lookup still finished in the background...
    t = time.monotonic()
    assert await g.for_picks([pick("b", 2)]) == {"b": {"google_id": "/m/0slow"}}  # ...and the next ask gets it at once
    assert time.monotonic() - t < 0.02 and tmdb.calls.count(2) == 1


async def test_prefetch_at_find_time_means_show_does_not_wait():
    tmdb = FakeTmdb({("tv", i): {"freebase_mid": f"/m/0{i}"} for i in (1, 2)}, delay=0.3)
    g = TitleIdResolver(tmdb, FakeWikidata({}), SERVICES, wait_s=0.05)
    g.prefetch([pick("a", 1), pick("b", 2), pick("v", 3, service="videoland")])  # never waits
    assert len(tmdb.calls) == 0  # started, not yet run
    await asyncio.sleep(0.35)  # Claude is choosing
    t = time.monotonic()
    assert await g.for_picks([pick("a2", 1), pick("b2", 2)]) == {
        "a2": {"google_id": "/m/01"},
        "b2": {"google_id": "/m/02"},
    }
    assert time.monotonic() - t < 0.02
    assert sorted(tmdb.calls) == [("tv", 1), ("tv", 2)]  # one lookup each, shared by find and show


async def test_show_joins_a_lookup_find_started():
    tmdb = FakeTmdb({("tv", 1): {"freebase_mid": "/m/01"}}, delay=0.2)
    g = TitleIdResolver(tmdb, FakeWikidata({}), SERVICES, wait_s=0.15)
    g.prefetch([pick("a", 1)])
    await asyncio.sleep(0.1)
    assert await g.for_picks([pick("a", 1)]) == {"a": {"google_id": "/m/01"}}  # 0.1 s left of 0.2 fits in the wait
    assert len(tmdb.calls) == 1


async def test_a_lookup_that_found_nothing_is_retried():
    tmdb = FakeTmdb({("tv", 1): RuntimeError("TMDB down")})
    g = TitleIdResolver(tmdb, FakeWikidata({}), SERVICES, wait_s=1)
    assert await g.for_picks([pick("a", 1)]) == {}
    tmdb.ext[("tv", 1)] = {"freebase_mid": "/m/01"}
    assert await g.for_picks([pick("a", 1)]) == {"a": {"google_id": "/m/01"}}
