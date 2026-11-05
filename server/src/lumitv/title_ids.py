"""Title ids other apps understand, for opening a pick at its exact page.

- `google_id`: Google Knowledge Graph id ("/g/11f7dr1t_l", "/m/0gg70vv") for Google TV's title page. Order: TMDB
  external_ids `freebase_mid`, else the title's Wikidata item: P2671 (Google Knowledge Graph ID), else P646
  (Freebase ID).
- `netflix_id`: Netflix title id ("70195800"), Wikidata P1874.

A service wants `google_id` when it has google_tv = true, and any id its launch template uses as a placeholder.
A miss, a timeout or an error only means that id is absent (the TV then skips the step that needs it).
"""

import asyncio
import json
import logging
import re
import time
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Protocol

import httpx

from .models import Kind
from .tmdb import Cache

if TYPE_CHECKING:
    from .models import TitleCard
    from .services import Service

log = logging.getLogger("lumitv.title_ids")

API_URL = "https://www.wikidata.org/w/api.php"
WIKIDATA_TTL_S = 30 * 24 * 3600  # ids rarely change
WIKIDATA_MISS_TTL_S = 7 * 24 * 3600  # a title without ids is looked up again sooner (new titles get ids later)
BATCH_SIZE = 50  # items per request (the API's limit for clients without bot rights)
BATCH_WINDOW_S = 0.05  # how long the first lookup waits for others to join its request
REQUEST_TIMEOUT_S = 10.0  # one batch request (a caller still waits at most its own timeout)
MAXLAG_S = 5  # Wikidata's etiquette: back off when its replicas lag
COOLDOWN_S = 600.0  # after a 403/429/maxlag, no requests for this long (or Retry-After if longer)
QID = re.compile(r"Q[1-9][0-9]*")
FORMATS = {"google_id": re.compile(r"/[gm]/[0-9a-z_]+"), "netflix_id": re.compile(r"[1-9][0-9]*")}
# Wikidata properties per id, in order of preference
PROPERTIES = {"google_id": ("P2671", "P646"), "netflix_id": ("P1874",)}

TitleIds = dict[str, str]  # id name ("google_id", "netflix_id") -> value


def _user_agent(contact: str = "") -> str:
    try:
        v = version("lumitv")
    except PackageNotFoundError:
        v = "0"
    contact = " ".join(contact.split())  # one header line
    # Wikimedia's robot policy form: client/version (contact) library/version. Without a contact Wikidata refuses.
    lib = f"python-httpx/{httpx.__version__}"
    return f"lumi-tv/{v} ({contact}) {lib}" if contact else f"lumi-tv/{v} {lib}"


def valid(name: str, value) -> str | None:
    return value if isinstance(value, str) and FORMATS[name].fullmatch(value) else None


def extract_ids(entity_data: dict) -> TitleIds:
    """Each id from an `{"entities": {...}}` response: the first valid value of its properties in order (preferred
    rank first, deprecated statements skipped). The single entity may sit under a redirect target's id."""
    out: TitleIds = {}
    for entity in (entity_data.get("entities") or {}).values():
        claims = entity.get("claims") or {}
        for name, props in PROPERTIES.items():
            for prop in props:
                if name in out:
                    break
                statements = [c for c in claims.get(prop) or [] if c.get("rank") != "deprecated"]
                statements.sort(key=lambda c: c.get("rank") != "preferred")
                for c in statements:
                    value = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
                    if v := valid(name, value):
                        out[name] = v
                        break
    return out


class Wikidata:
    """Ids per Wikidata item, cached in the shared SQLite cache, misses too (for a shorter time). Never raises.

    Wikidata's robot policy forbids crawling (it answered 403 to one-item-per-request bursts from the warm job), so
    lookups are batched: callers queue item ids, and one request at a time asks the API for up to BATCH_SIZE items
    (`wbgetentities` with `maxlag`), newest first so a pick being shown is not stuck behind the warm job. After a
    refusal (403/429/maxlag) no request is sent for COOLDOWN_S; cached ids (stale too) still answer."""

    def __init__(self, cache: Cache, timeout_s: float = 2.0, ttl_s: float = WIKIDATA_TTL_S,
                 miss_ttl_s: float = WIKIDATA_MISS_TTL_S, contact: str = "",
                 client: httpx.AsyncClient | None = None):  # fmt: skip
        self._cache = cache
        self._ttl_s = ttl_s
        self._miss_ttl_s = miss_ttl_s
        self._timeout_s = timeout_s
        self._pending: dict[str, asyncio.Future[TitleIds | None]] = {}  # qid -> result (None: fetch failed)
        self._worker: asyncio.Task[None] | None = None
        self._paused_until = 0.0  # monotonic time; no requests before it
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": _user_agent(contact), "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT_S,
        )

    async def aclose(self) -> None:
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        await self._client.aclose()

    async def ids(self, qid: str) -> TitleIds:
        if not QID.fullmatch(qid or ""):
            return {}
        cached = self._cache.get(self._key(qid))
        if cached:
            found = json.loads(cached[0])
            if self._cache.is_fresh(cached[1], self._ttl_s if found else self._miss_ttl_s):
                return found
        stale = json.loads(cached[0]) if cached else {}
        if time.monotonic() < self._paused_until:
            return stale
        fut = self._pending.get(qid)
        if fut is None:
            fut = self._pending[qid] = asyncio.get_running_loop().create_future()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._drain())
        try:  # shielded: a caller giving up does not drop the lookup, its result still lands in the cache
            result = await asyncio.wait_for(asyncio.shield(fut), self._timeout_s)
        except TimeoutError:
            return stale
        return stale if result is None else result

    @staticmethod
    def _key(qid: str) -> str:
        return f"wikidata:{qid}:ids"

    async def _drain(self) -> None:
        """Fetch queued items, one request at a time, until the queue is empty."""
        await asyncio.sleep(BATCH_WINDOW_S)  # let a burst of lookups (find_titles' candidates) share a request
        while self._pending:
            if time.monotonic() < self._paused_until:
                self._settle(list(self._pending), None)
                return
            batch = list(self._pending)[-BATCH_SIZE:]  # newest first
            found = await self._fetch(batch)
            if found is None:  # down or refusing: don't fire the rest of the queue at it; later lookups retry
                self._settle(list(self._pending), None)
                return
            for qid in batch:
                self._cache.put(self._key(qid), json.dumps(found.get(qid, {})))
            self._settle(batch, found)

    def _settle(self, qids: list[str], found: dict[str, TitleIds] | None) -> None:
        for qid in qids:
            fut = self._pending.pop(qid, None)
            if fut and not fut.done():
                fut.set_result(None if found is None else found.get(qid, {}))

    async def _fetch(self, qids: list[str]) -> dict[str, TitleIds] | None:
        """Ids per asked item (a missing item maps to nothing); None when the request failed (nothing is cached)."""
        asked = list(qids)
        missing: set[str] = set()
        for _ in range(3):  # an id Wikidata rejects fails the whole request: drop it and ask again
            try:
                resp = await self._client.get(API_URL, params={
                    "action": "wbgetentities", "ids": "|".join(asked), "props": "claims",
                    "format": "json", "maxlag": str(MAXLAG_S),
                })  # fmt: skip
                if resp.status_code in (403, 429):
                    self._pause(f"HTTP {resp.status_code}", resp.headers.get("retry-after"))
                    return None
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                log.info("wikidata batch of %d failed: %r", len(asked), e)
                return None
            err = data.get("error") or {}
            if err.get("code") == "maxlag":
                self._pause("maxlag", resp.headers.get("retry-after"))
                return None
            if err.get("code") == "no-such-entity" and err.get("id") in asked:
                missing.add(err["id"])
                asked.remove(err["id"])
                if asked:
                    continue
                return {}
            if err:
                log.info("wikidata batch of %d failed: %s", len(asked), err.get("code"))
                return None
            return self._by_asked_id(asked, data.get("entities") or {})
        return None

    @staticmethod
    def _by_asked_id(asked: list[str], entities: dict) -> dict[str, TitleIds]:
        """Map each asked id to its entity's ids; a merged item comes back under its target (with `redirects`)."""
        by_id: dict[str, dict] = {}
        for key, ent in entities.items():
            if not isinstance(ent, dict) or "missing" in ent:
                continue
            by_id[key] = ent
            if isinstance(src := (ent.get("redirects") or {}).get("from"), str):
                by_id[src] = ent
        return {q: extract_ids({"entities": {q: by_id[q]}}) for q in asked if q in by_id}

    def _pause(self, why: str, retry_after: str | None) -> None:
        wait = COOLDOWN_S
        if retry_after and retry_after.isdigit():
            wait = max(wait, float(retry_after))
        self._paused_until = time.monotonic() + wait
        log.warning("wikidata refused (%s): no lookups for %d s; cached ids still used", why, wait)


class ExternalIds(Protocol):
    async def external_ids(self, kind: Kind, tmdb_id: int) -> dict: ...


class TitleIdResolver:
    """Resolves the ids each pick's service wants; misses and failures are just absent. Lookups are shared: one
    started by [prefetch] when find_titles returns is the one show_on_tv/open_title wait for."""

    MAX_TRACKED = 500  # finished lookups kept in memory (the SQLite cache keeps the rest)

    def __init__(self, tmdb: ExternalIds, wikidata: Wikidata, services: "dict[str, Service]", wait_s: float):
        self._tmdb = tmdb
        self._wikidata = wikidata
        self._services = services
        self._wait_s = wait_s
        self._lookups: dict[tuple[Kind, int, frozenset[str]], asyncio.Task[TitleIds]] = {}

    async def resolve(self, kind: Kind, tmdb_id: int, wanted: frozenset[str]) -> TitleIds:
        """The `wanted` ids found for a title. Wikidata is asked only for ids TMDB doesn't carry."""
        if not wanted:
            return {}
        try:
            ext = await self._tmdb.external_ids(kind, tmdb_id)
            out: TitleIds = {}
            if "google_id" in wanted and (gid := valid("google_id", ext.get("freebase_mid"))):
                out["google_id"] = gid
            qid = ext.get("wikidata_id")
            if wanted - out.keys() and isinstance(qid, str):
                found = await self._wikidata.ids(qid)
                out |= {k: v for k, v in found.items() if k in wanted and k not in out}
            return out
        except Exception as e:  # TMDB down, bad data: no ids
            log.info("title ids for %s %s failed: %r", kind, tmdb_id, e)
            return {}

    def wanted(self, title: "TitleCard") -> frozenset[str]:
        sid = title.primary_service
        svc = self._services.get(sid) if sid else None
        return svc.wanted_ids if svc else frozenset()

    def _lookup(self, title: "TitleCard") -> "asyncio.Task[TitleIds] | None":
        """The running or finished lookup for a title, started if there is none (or the last one found nothing:
        that may have been a transient failure, and misses are cached below anyway)."""
        wanted = self.wanted(title)
        if not wanted:
            return None
        key = (title.kind, title.tmdb_id, wanted)
        task = self._lookups.get(key)
        if task is None or (task.done() and (task.cancelled() or not task.result())):
            task = asyncio.ensure_future(self.resolve(title.kind, title.tmdb_id, wanted))
            self._lookups.pop(key, None)
            self._lookups[key] = task
            while len(self._lookups) > self.MAX_TRACKED:
                oldest = next(iter(self._lookups))
                if not self._lookups[oldest].done():
                    break
                del self._lookups[oldest]
        return task

    def prefetch(self, titles: "list[TitleCard]") -> None:
        """Start the lookups for find_titles' candidates in the background; never waits."""
        for t in titles:
            self._lookup(t)

    async def for_picks(self, picks: "list[TitleCard]") -> dict[str, TitleIds]:
        """token -> ids for the picks whose service wants any. Waits at most wait_s; lookups still running then
        finish in the background (and land in the cache) but are left out."""
        tasks = {p.token: t for p in picks if (t := self._lookup(p)) is not None}
        if not tasks:
            return {}
        await asyncio.wait(tasks.values(), timeout=self._wait_s)
        return {tok: t.result() for tok, t in tasks.items() if t.done() and not t.cancelled() and t.result()}
