import asyncio
import itertools
import json
import math
import re
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .catalog_source import CatalogSource, Details
from .config import EXCLUDE_TMDB_FORMAT, Ranking
from .kids import KidsPolicy, allowed, cert_cutoff, effective_age
from .models import Kind, ServiceId, TitleCard
from .services import Service

PICK_TTL_S = 2 * 3600
HOURS_DISCOVER_PAGES = 2  # discover pages read when min_hours/max_hours is set
ESTIMATE_MARGIN = 0.3  # estimated series length within ±30% of the range gets an exact lookup


class Pick(TitleCard):
    kids_mode: bool
    kids_age: int | None
    turn_id: str | None = None
    question: str | None = None
    issued_at: float = 0.0  # clock reading, for TTL only
    seq: int  # issue order; clocks can tie, so recency uses this


class PickRegistry:
    """In-memory token -> Pick, expiring after PICK_TTL_S."""

    def __init__(self, ttl_s: float = PICK_TTL_S, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_s
        self._clock = clock
        self._seq = itertools.count(1)
        self._picks: dict[str, tuple[float, Pick]] = {}

    def issue(
        self,
        card_data: dict,
        kids_mode: bool,
        kids_age: int | None,
        turn_id: str | None = None,
        question: str | None = None,
    ) -> str:
        now = self._clock()
        self._picks = {t: (exp, p) for t, (exp, p) in self._picks.items() if exp > now}
        token = secrets.token_urlsafe(6)  # 8 urlsafe chars
        while token in self._picks:
            token = secrets.token_urlsafe(6)
        data = {k: v for k, v in card_data.items() if k != "token"}
        pick = Pick(
            token=token,
            **data,
            kids_mode=kids_mode,
            kids_age=kids_age,
            turn_id=turn_id,
            question=question,
            issued_at=now,
            seq=next(self._seq),
        )
        self._picks[token] = (now + self._ttl, pick)
        return token

    def get(self, token: str) -> Pick | None:
        entry = self._picks.get(token)
        if entry is None:
            return None
        if entry[0] <= self._clock():
            del self._picks[token]
            return None
        return entry[1]


@dataclass
class LookupStats:
    """Failures find_titles swallowed on purpose (and the lookups that worked, so an outage can be told from a thin
    result); the tool wrapper logs them as counts and tells Claude through `degraded`."""

    inputs_failed: int = 0  # searches, similar lists, discovers, genre lists, keyword lookups
    inputs_ok: int = 0
    details_failed: int = 0
    details_ok: int = 0
    unknown_genres: list[str] = field(default_factory=list)  # asked-for genre names no searched kind has
    skipped_kinds: list[str] = field(default_factory=list)  # kinds left out because they lack a requested genre
    no_titles_on: list[str] = field(default_factory=list)  # service ids with no matching title (no service asked)

    def degraded(self) -> list[str]:
        """Why the answer may fall short, for Claude: [] when every lookup worked; "tmdb_unavailable" when a whole
        stage got nothing back (the catalogue, not the question, is the reason); else "lookups_failed:<n>"."""
        failed = self.inputs_failed + self.details_failed
        if not failed:
            return []
        if (self.inputs_failed and not self.inputs_ok) or (self.details_failed and not self.details_ok):
            return ["tmdb_unavailable"]
        return [f"lookups_failed:{failed}"]


# TMDB's mood keywords say how a title feels, not what it is about: "exciting" alone ties Star Trek to Lupin.
MOOD_KEYWORDS = frozenset([
    "absurd", "admiring", "adoring", "aggressive", "amazed", "amused", "angry", "antagonistic", "anxious",
    "appreciative", "approving", "arrogant", "assertive", "audacious", "awestruck", "baffled", "bewildered", "bold",
    "callous", "candid", "cautionary", "celebratory", "cheerful", "comforting", "compassionate", "complicated",
    "critical", "cruel", "curious", "defiant", "depressing", "desperate", "dignified", "disdainful", "dismal",
    "distressing", "dramatic", "dreary", "earnest", "embarrassed", "empathetic", "enchant", "energetic",
    "enthusiastic", "euphoric", "excited", "exciting", "exhilarated", "exuberant", "familiar", "farcical", "frantic",
    "frightened", "harsh", "hilarious", "hopeful", "hopeless", "horrified", "humorous", "idyllic", "imaginative",
    "incredulous", "informative", "insightful", "inspirational", "intense", "intimate", "introspective", "ironic",
    "joyful", "joyous", "kinetic", "lighthearted", "loving", "malicious", "melancholy", "mischievous", "mocking",
    "mysterious", "nostalgic", "ominous", "optimistic", "pessimistic", "playful", "powerful", "pretentious",
    "provocative", "proud", "quirky", "rebellious", "reflective", "relaxed", "respectful", "romantic", "sad",
    "sarcastic", "sardonic", "scary", "sensational", "sentimental", "serious", "shocking", "silly", "sincere",
    "skeptical", "somber", "suspenseful", "sweet", "sympathetic", "tense", "thoughtful", "tragic", "unsettling",
    "vibrant", "whimsical", "wistful", "witty", "wonder", "cerebral", "urgent", "ambiguous", "clever", "gritty",
    "bittersweet", "uplifting", "heartwarming", "grim", "bleak", "edgy", "factual", "grand", "bombastic",
    "electrifying", "ambivalent", "apathetic", "apprehensive", "annoyed", "condescending", "cynical", "disgusted",
    "disheartening", "enraged", "frustrated", "gloomy", "hostile", "hostility", "outraged", "vindictive", "zealous",
    "tender", "triumphant", "grateful", "jovial", "panicked", "wary",
]) | {"matter of fact", "mean spirited", "straightforward", "feel good"}  # fmt: skip
KEYWORD_MAX_TITLES = 3000  # keywords on more titles than this ("friendship", "based on novel or book") say nothing
# Rarer than this counts as this rare: one very rare shared keyword ("gnosticism") must not outweigh several
# telling ones ("space opera", "space travel")
KEYWORD_RARE_FLOOR = 30
# Searched keywords must be on at least this many titles: rarer ones ("gnosticism", "romulans") find little on our
# services (and the franchise, which search already finds by name). They still count when ranking.
KEYWORD_SEARCH_MIN = KEYWORD_RARE_FLOOR
KEYWORDS_SEARCHED = 8  # a liked title's strongest searchable keywords, OR-ed in one discover per service and kind
KEYWORDS_COMMON_SEARCHED = 4  # plus its most common usable ones, OR-ed in a discover of their own
COMMON_POOL_MAX = 5  # titles found by the common keywords only, looked up beyond the pool cap
COMMON_SHARED = 2  # ... and ranked after the rest unless they share this many keywords with the liked titles
LIKED_DETAILS_TRIES = 2
FRANCHISE_SHARED = 2  # a title named like the liked one is its franchise only when it also shares this many keywords
FRANCHISE_MAX = 12  # franchise titles looked up beyond the pool cap (most of a long franchise isn't on our services)


def searched_keywords(weights: dict[int, float], counts: dict[int, int]) -> tuple[list[int], list[int]]:
    """The liked titles' keywords worth a discover, as two searches: the KEYWORDS_SEARCHED rarest ("cyberpunk",
    "super villain") and the KEYWORDS_COMMON_SEARCHED most common usable ones. The common ones are often what the
    title *is* ("superhero", "based on comic"): rarity ranks them last, yet they find The Boys for Spider-Man. They
    are searched apart because discover returns the most popular page of an OR, and popular superhero titles would
    push Blade Runner ("cyberpunk") out of The Matrix's."""
    usable = sorted((k for k in weights if counts[k] >= KEYWORD_SEARCH_MIN), key=lambda k: -weights[k])
    rare = usable[:KEYWORDS_SEARCHED]
    common = [k for k in reversed(usable) if k not in rare][:KEYWORDS_COMMON_SEARCHED]
    return rare, common


@dataclass
class LikeSignals:
    """What a "like X" search learned, for ranking its candidates once their details (keywords) are known."""

    franchise: set[tuple[int, Kind]] = field(default_factory=set)  # named like a liked title ("Star Trek: Picard")
    recommended: dict[tuple[int, Kind], int] = field(default_factory=dict)  # liked titles whose TMDB similar has it
    weights: dict[int, float] = field(default_factory=dict)  # liked titles' usable keywords: id -> weight (rarer: more)
    names: dict[int, str] = field(default_factory=dict)  # keyword id -> name
    liked_genres: dict[Kind, dict[int, int]] = field(default_factory=dict)  # _liked_genre_counts per kind
    n_liked: int = 0
    common_only: set[tuple[int, Kind]] = field(default_factory=set)  # found by a common keyword and nothing else


def _named_like(name: str, row: dict) -> bool:
    """The row's title is the liked name, starts with it as whole words ("Star Trek: Picard" for "Star Trek"), or
    has it in a subtitle ("El Camino: A Breaking Bad Movie")."""
    n = _norm_title(name)
    if not n:
        return False
    for f in TITLE_FIELDS:
        t = _norm_title(row.get(f))
        sub = _norm_title((row.get(f) or "").partition(":")[2])
        if t == n or t.startswith(n + " ") or f" {n} " in f" {sub} ":
            return True
    return False


async def _safe_count(source: CatalogSource, keyword_id: int, stats: LookupStats | None) -> int:
    try:
        n = await source.keyword_count(keyword_id)
    except Exception:
        if stats:
            stats.inputs_failed += 1
        return 0
    if stats:
        stats.inputs_ok += 1
    return n


def keyword_weights(names: dict[int, str], counts: dict[int, int]) -> dict[int, float]:
    """Usable keywords and how much each says: mood words and keywords on more than KEYWORD_MAX_TITLES titles (or
    an unknown count) are dropped; the rest weigh log(1 + KEYWORD_MAX_TITLES / max(count, KEYWORD_RARE_FLOOR)), so
    rarer counts more, up to a point."""
    return {
        k: math.log(1 + KEYWORD_MAX_TITLES / max(counts[k], KEYWORD_RARE_FLOOR))
        for k, n in names.items()
        if n.lower() not in MOOD_KEYWORDS and 0 < counts.get(k, 0) <= KEYWORD_MAX_TITLES
    }


def parse_excludes(entries: Iterable[int | str]) -> tuple[set[int], set[tuple[int, Kind]]]:
    """exclude_tmdb_ids as (bare ids, (id, kind) pairs). "movie:<id>" / "tv:<id>" excludes one title; a bare id (or
    a string of digits) excludes both kinds, since film and series ids overlap. ValueError names a bad entry."""
    bare: set[int] = set()
    pairs: set[tuple[int, Kind]] = set()
    for e in entries:
        text = str(e).strip()
        if text.isdigit():
            bare.add(int(text))
        elif EXCLUDE_TMDB_FORMAT.fullmatch(text):
            kind, i = text.split(":")
            pairs.add((int(i), kind))  # type: ignore[arg-type]
        else:
            raise ValueError(f'exclude_tmdb_ids entry {e!r} is not a tmdb id, "movie:<id>" or "tv:<id>"')
    return bare, pairs


async def _safe(coro, stats: LookupStats | None = None) -> list[dict]:
    try:
        rows = await coro
    except Exception:
        if stats:
            stats.inputs_failed += 1
        return []
    if stats:
        stats.inputs_ok += 1
    return rows


def load_genre_aliases(content_dir: Path, language: str) -> dict[str, list[str]]:
    """Alias -> TMDB genre names to try in order (content/genre_aliases.<language>.json); none if no file."""
    path = content_dir / f"genre_aliases.{language}.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k.lower(): list(v) for k, v in raw.items()}


def load_genre_keywords(content_dir: Path, language: str) -> dict[str, list[str]]:
    """Genre name -> TMDB keyword names a series asked for under it must carry one of
    (content/genre_keywords.<lang>.json)."""
    path = content_dir / f"genre_keywords.{language}.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k.lower(): list(v) for k, v in raw.items() if not k.startswith("_")}


async def genre_ids(
    source: CatalogSource,
    names: list[str],
    aliases: dict[str, list[str]],
    kinds: list[Kind],
    stats: LookupStats | None,
) -> dict[Kind, list[int]]:
    """Per kind, the genre ids for `names` (same order). A kind that lacks any of them is left out, since
    discover would otherwise return titles missing a genre the user asked for. Unmatched names -> stats."""
    lists = await asyncio.gather(*(_safe_genres(source, k, stats) for k in kinds))
    by_kind = {
        k: {n.lower(): i for n, i in lst.items()} for k, lst in zip(kinds, lists, strict=True) if lst is not None
    }
    if not by_kind:
        return {}
    ids: dict[Kind, list[int]] = {k: [] for k in by_kind}
    complete = dict.fromkeys(by_kind, True)
    for name in names:
        targets = [name.lower(), *(t.lower() for t in aliases.get(name.lower(), []))]
        hits = {k: next((t[x] for x in targets if x in t), None) for k, t in by_kind.items()}
        if all(h is None for h in hits.values()):  # unknown everywhere: ignored, and reported
            if stats is not None and name not in stats.unknown_genres:
                stats.unknown_genres.append(name)
            continue
        for k, hit in hits.items():
            if hit is None:
                complete[k] = False
            elif hit not in ids[k]:
                ids[k].append(hit)
    if stats is not None:
        stats.skipped_kinds += [k for k in by_kind if not complete[k] and k not in stats.skipped_kinds]
    return {k: v for k, v in ids.items() if complete[k] and v}


async def _safe_genres(source: CatalogSource, kind: Kind, stats: LookupStats | None) -> dict[str, int] | None:
    try:
        lists = await source.genres(kind)
    except Exception:
        if stats:
            stats.inputs_failed += 1
        return None
    if stats:
        stats.inputs_ok += 1
    return lists


def _matching_services(details: Details, services: dict[str, Service]) -> list[ServiceId]:
    return [
        s.id
        for s in services.values()
        if s.region and s.tmdb_provider_ids and s.tmdb_provider_ids & details.flatrate_by_region.get(s.region, set())
    ]


def _spans(d: Details) -> list[tuple[int | None, int]]:
    """A series' lengths that can satisfy an hours filter, in match order: (None = whole run, minutes),
    then first season, then first two seasons. Spans with unknown minutes are left out."""
    spans: list[tuple[int | None, int]] = []
    if d.total_minutes is not None:
        spans.append((None, d.total_minutes))
    if d.season1_minutes is not None:
        spans.append((1, d.season1_minutes))
        if d.season2_minutes is not None:
            spans.append((2, d.season1_minutes + d.season2_minutes))
    return spans


async def _liked_genre_counts(
    source: CatalogSource,
    liked: list[dict],
    target: Kind,
    aliases: dict[str, list[str]],
    stats: LookupStats | None,
) -> dict[int, int]:
    """The liked titles' genres (ids on their search rows) as `target`-kind genre ids, matched by name or
    alias (movie "Science Fiction" -> tv "Sci-Fi & Fantasy"), with how many liked titles have each, in
    first-seen order. Unmatched are dropped."""
    kinds = list(dict.fromkeys([target, *(h["media_type"] for h in liked)]))
    lists = dict(zip(kinds, await asyncio.gather(*(_safe_genres(source, k, stats) for k in kinds)), strict=True))
    target_ids = {n.lower(): i for n, i in (lists.get(target) or {}).items()}
    counts: dict[int, int] = {}
    for h in liked:
        names = {i: n for n, i in (lists.get(h["media_type"]) or {}).items()}
        mapped: set[int] = set()
        for gid in h.get("genre_ids") or []:
            name = names.get(gid, "").lower()
            hit = next(
                (target_ids[x] for x in [name, *(a.lower() for a in aliases.get(name, []))] if x in target_ids), None
            )
            if hit is not None and hit not in mapped:
                mapped.add(hit)
                counts[hit] = counts.get(hit, 0) + 1
    return counts


def _bridged_genre_ids(counts: dict[int, int], n_liked: int) -> list[int]:
    """Genres to bridge by, from _liked_genre_counts; most liked titles first."""
    # OR-joined, so a genre only one of several liked titles has (Star Trek's "Drama") would flood the
    # results; keep the genres at least half of them share, when there are any
    shared = [g for g in counts if counts[g] * 2 >= n_liked]
    return sorted(shared or counts, key=lambda g: -counts[g])  # stable: ties keep first-seen order


NO_RANKING = Ranking(
    off_genres_unless_liked=[], min_votes=0, kids_min_votes=0
)  # no filtering; scoring weights stay at their defaults


def _genre_fit(genre_ids: list[int], counts: dict[int, int], n_liked: int) -> float:
    """How closely a title's genres match the liked titles', 0..1: weighted Jaccard, each liked genre weighted
    by the share of liked titles that have it. Genres the title has but the liked titles lack count against it
    (Kill Bill's Crime and Thriller against Spider-Man's Action and Science Fiction)."""
    if not n_liked:
        return 0.0
    mine = set(genre_ids)
    weight = {g: n / n_liked for g, n in counts.items()}
    shared = sum(w for g, w in weight.items() if g in mine)
    union = len(mine) + sum(w for g, w in weight.items() if g not in mine)
    return shared / union if union else 0.0


TITLE_FIELDS = ("title", "name", "original_title", "original_name")  # movie and tv search rows


def _norm_title(t: str | None) -> str:
    """Case- and punctuation-insensitive form of a title: "Spider-Man: No Way Home" -> "spider man no way home"."""
    return " ".join(re.sub(r"[^\w]+", " ", (t or "").casefold()).split())


def _quality(row: dict, rk: Ranking) -> float:
    """TMDB vote_average pulled toward the mean by how few votes it has: (v*R + m*C) / (v + m)."""
    v, r = row.get("vote_count") or 0, row.get("vote_average") or 0.0
    m = rk.bayes_min_votes
    return (v * r + m * rk.bayes_mean) / (v + m) if v + m else rk.bayes_mean


async def _candidates(
    source: CatalogSource,
    services: dict[str, Service],
    query: str | None,
    like: list[str] | None,
    service: ServiceId | None,
    kind: Kind | None,
    policy: KidsPolicy,
    kids_mode: bool,
    kids_age: int | None,
    stats: LookupStats | None = None,
    genres: list[str] | None = None,
    genre_aliases: dict[str, list[str]] | None = None,
    limit: int = 10,
    pages: int = 1,
    ranking: Ranking = NO_RANKING,
    exclude: set[int] | None = None,
    cap: int | None = None,
    signals: LikeSignals | None = None,
    genre_keywords: dict[str, list[str]] | None = None,
    exclude_pairs: set[tuple[int, Kind]] | None = None,
) -> list[tuple[int, Kind]]:
    """`exclude`: tmdb ids left out whatever their kind; `exclude_pairs`: (id, kind) left out. `signals`: filled for
    a `like` search, for find_titles' final ranking."""
    found: list[list[tuple[int, Kind]]] = []
    genre_found: list[list[tuple[int, Kind]]] = []  # ranked ahead of query/like matches
    bridged: list[list[tuple[int, Kind]]] = []  # like across kinds: interleaved after same-kind similar
    common_found: list[list[tuple[int, Kind]]] = []  # the lists in `bridged` from the common-keyword discover
    common_only: set[tuple[int, Kind]] = set()  # titles only that discover found (set below, outside kids mode)
    cutoff = cert_cutoff(kids_age, policy) if kids_mode else None
    active = [s for s in services.values() if s.tmdb_provider_ids and s.region and (service is None or s.id == service)]

    meta: dict[tuple[int, Kind], dict] = {}  # first row seen per title: genre_ids and votes, for filtering and ranking

    def rows(rs: list[dict], fallback: Kind | None) -> list[tuple[int, Kind]]:
        out = []
        for r in rs:
            k = r.get("media_type") or fallback
            if k in ("movie", "tv") and "id" in r and not r.get("adult"):  # adult titles: never, in any mode
                out.append((r["id"], k))
                meta.setdefault((r["id"], k), r)
        return out

    async def discover(
        kinds: list[Kind],
        ids: dict[Kind, list[int]] | None,
        any_genre: bool,
        votes: int | None = None,
        keywords: list[int] | None = None,
    ) -> list[list[tuple[int, Kind]]]:
        """One list per service x kind, pages 1..`pages` in order."""

        async def one(s: Service, k: Kind) -> list[tuple[int, Kind]]:
            got = await asyncio.gather(*(
                _safe(
                    source.discover(
                        k, s.region, set(s.tmdb_provider_ids), ids[k] if ids else None, cutoff, page=p,
                        any_genre=any_genre, min_votes=votes, **({"keywords": keywords} if keywords else {}),
                    ),
                    stats,
                )
                for p in range(1, pages + 1)
            ))  # fmt: skip
            return [r for page_rows in got for r in rows(page_rows, k)]

        return list(await asyncio.gather(*(one(s, k) for s in active for k in kinds)))

    like_ids: set[tuple[int, Kind]] = set()
    liked: list[dict] = []
    profiles: dict[Kind, asyncio.Future[dict[int, int]]] = {}
    sims: list[list[tuple[int, Kind]]] = []
    allowed_genres: set[int] = set()  # genres the user asked for, exempt from the off-genre filter
    query_found: list[tuple[int, Kind]] = []
    franchise: list[tuple[int, Kind]] = []  # titles named like a liked one, ahead of the rest of the pool
    if like:

        async def lookup(name: str) -> tuple[dict | None, list[dict]]:  # the liked title may be any kind
            hits = [
                r
                for r in await _safe(source.search(name, None), stats)
                if r.get("media_type") in ("movie", "tv") and "id" in r
            ]
            # search ranks by popularity, so "The Avengers" leads with a 1961 series; people mean the well-known
            # one. Among the top hits, those named exactly as asked win ("Toy Story 4", not the more-voted
            # "Toy Story"); the most-voted of them, else the most-voted top hit. Ties: the first.
            top = hits[: max(1, ranking.liked_from_top)]
            exact = [r for r in top if _norm_title(name) in {_norm_title(r.get(f)) for f in TITLE_FIELDS}]
            return (max(exact or top, key=lambda r: r.get("vote_count") or 0) if top else None), hits

        looked = await asyncio.gather(*(lookup(n) for n in like))
        liked = [h for h, _ in looked if h]
        like_ids = {(h["id"], h["media_type"]) for h in liked}
        # "something like Star Trek" usually means the franchise too: its other series and films are the closest
        # matches there are, and search finds them by name
        franchise = list(dict.fromkeys(
            c for name, (h, hits) in zip(like, looked, strict=True) if h
            for c in rows([r for r in hits if _named_like(name, r)], None)
            if c not in like_ids and (kind is None or c[1] == kind)
        ))  # fmt: skip
        same_kind = [h for h in liked if kind is None or h["media_type"] == kind]

        async def similar(h: dict) -> list[tuple[int, Kind]]:
            sim = rows(await _safe(source.similar(h["id"], h["media_type"]), stats), h["media_type"])
            return [r for r in sim if kind is None or r[1] == kind]

        def liked_genres(k: Kind) -> asyncio.Future[dict[int, int]]:  # per kind, fetched once for bridge and score
            if k not in profiles:
                profiles[k] = asyncio.ensure_future(_liked_genre_counts(source, liked, k, genre_aliases or {}, stats))
            return profiles[k]

        async def bridge() -> list[list[tuple[int, Kind]]]:
            async def one(k: Kind) -> list[list[tuple[int, Kind]]]:
                ids = _bridged_genre_ids(await liked_genres(k), len(liked))
                return (
                    await discover(
                        [k], {k: ids}, any_genre=True, votes=None if kids_mode else ranking.min_votes or None
                    )
                    if ids
                    else []
                )

            return [
                f for fs in await asyncio.gather(*(one(k) for k in ([kind] if kind else ["movie", "tv"]))) for f in fs
            ]

        async def keywords() -> tuple[dict[int, str], dict[int, float], dict[int, int]]:
            """The liked titles' keywords (id -> name), the usable ones' weights, and how many titles have each."""

            async def of(h: dict) -> dict[int, str]:
                # The whole answer hangs on this lookup (without it, only genres are left), so a slow TMDB or a
                # busy server gets a second try.
                for _attempt in range(LIKED_DETAILS_TRIES):
                    try:
                        d = await source.details(h["id"], h["media_type"])
                        break
                    except Exception:
                        if stats:
                            stats.inputs_failed += 1
                else:
                    return {}
                if stats:
                    stats.inputs_ok += 1
                if d.genre_ids:  # fuller than the search row's (Star Trek's row lacks Action & Adventure)
                    h["genre_ids"] = list(dict.fromkeys([*d.genre_ids, *(h.get("genre_ids") or [])]))
                return d.keywords

            names: dict[int, str] = {}
            for m in await asyncio.gather(*(of(h) for h in liked)):
                names.update(m)
            usable = [k for k, n in names.items() if n.lower() not in MOOD_KEYWORDS]
            counts = dict(
                zip(usable, await asyncio.gather(*(_safe_count(source, k, stats) for k in usable)), strict=True)
            )
            return names, keyword_weights(names, counts), counts

        # Shared keywords find what TMDB's genres can't: "space opera" and "starship" instead of every
        # "Drama, Sci-Fi & Fantasy" title. Genre bridging stays for liked titles without usable keywords.
        kw_names, weights, kw_counts = await keywords()
        rare_kw, common_kw = searched_keywords(weights, kw_counts)
        searched = rare_kw + common_kw
        if signals is not None:
            signals.names, signals.weights, signals.n_liked = kw_names, weights, len(liked)

        async def keyword_discovers(rare: list[int], common: list[int]) -> list[list[tuple[int, Kind]]]:
            rare_lists, common_lists = await asyncio.gather(
                discover(kinds_wanted, None, any_genre=False, votes=votes, keywords=rare)
                if rare
                else asyncio.sleep(0, []),
                discover(kinds_wanted, None, any_genre=False, votes=votes, keywords=common)
                if common
                else asyncio.sleep(0, []),
            )
            common_found.extend(common_lists)
            return rare_lists + common_lists

        # TMDB similar stays in the liked title's kind, so a liked film can't yield series: bridge by genre.
        # With no kind asked, always bridge both kinds: similar rows aren't limited to our services (Spider-Man's
        # are mostly Marvel titles we can't play), so their count says nothing about how many will survive.
        crosses = kind is not None and len(same_kind) < len(liked)
        kinds_wanted: list[Kind] = [kind] if kind else ["movie", "tv"]
        votes = None if kids_mode else ranking.min_votes or None
        sims, bridged = await asyncio.gather(
            asyncio.gather(*(similar(h) for h in same_kind)),
            keyword_discovers(rare_kw, common_kw) if searched
            else bridge() if crosses or (kind is None and liked) else asyncio.sleep(0, []),
        )  # fmt: skip
        found += list(sims)
        allowed_genres |= {g for h in liked for g in h.get("genre_ids") or []}
        if (
            not searched
            and kind is not None
            and not crosses
            and liked
            and len({r for f in sims for r in f} - like_ids) < limit
        ):
            bridged = await bridge()
    if query:
        query_found = rows(await _safe(source.search(query, kind), stats), kind)
        found.append(query_found)
    if genres or (not like and not query):
        kinds: list[Kind] = [kind] if kind else ["movie", "tv"]
        by_kind_ids: dict[Kind, list[int]] | None = None
        if genres:
            by_kind_ids = await genre_ids(source, genres, genre_aliases or {}, kinds, stats)
            kinds = [k for k in kinds if k in by_kind_ids]
            allowed_genres |= {g for ids in by_kind_ids.values() for g in ids}
        # a vote floor: by popularity alone, this week's barely rated releases lead ("something funny" -> a 2026
        # film with 12 votes); kids' titles get fewer votes, so a lower floor there
        floor = (ranking.kids_min_votes if kids_mode else ranking.min_votes) or None
        # series have one Sci-Fi & Fantasy genre: "sci-fi" there must also carry a sci-fi keyword (genre_keywords)
        series_kw_names = list(
            dict.fromkeys(n for g in genres or [] for n in (genre_keywords or {}).get(g.lower(), []))
        )
        series_kw: list[int] = []
        if series_kw_names and "tv" in kinds:
            try:
                series_kw = list((await source.keyword_ids(series_kw_names)).values())
            except Exception:
                if stats:
                    stats.inputs_failed += 1
            else:
                if stats:
                    stats.inputs_ok += 1
        if series_kw:
            lists = await asyncio.gather(
                discover([k for k in kinds if k != "tv"], by_kind_ids, any_genre=False, votes=floor),
                discover(["tv"], by_kind_ids, any_genre=False, votes=floor, keywords=series_kw),
            )
            (genre_found if genres else found).extend(lists[0] + lists[1])
        else:
            (genre_found if genres else found).extend(await discover(kinds, by_kind_ids, any_genre=False, votes=floor))

    seen_ids: set[tuple[int, Kind]] = set()
    merged: list[tuple[int, Kind]] = []
    # interleave inputs so every like/service contributes before the cap applies. Bridged lists share a group
    # with similar (each round: similar first): similar isn't limited to our services, so ranking all of it
    # first would fill the cap with titles that get dropped
    for group in (genre_found, [franchise], found + bridged):
        for i in range(max((len(f) for f in group), default=0)):
            for f in group:
                if i < len(f) and f[i] not in seen_ids and f[i] not in like_ids:
                    seen_ids.add(f[i])
                    merged.append(f[i])

    excluded_set = exclude or set()
    blocked = ranking.blocked() | (
        exclude_pairs or set()
    )  # the config blocklist: every mode, even a title the user named
    merged = [c for c in merged if c[0] not in excluded_set and c not in blocked]
    if not kids_mode and ranking.off_genres_unless_liked:  # a title the user named (query) is never dropped
        drop = set(ranking.off_genres_unless_liked) - allowed_genres
        named = set(query_found)
        merged = [c for c in merged if c in named or not drop & set(meta[c].get("genre_ids") or [])]
    if like and not kids_mode:  # kids mode keeps the plain interleave order
        in_genre = {c for f in genre_found for c in f}  # requested genres lead, as before; scoring orders within
        # Common-keyword finds ("superhero") are kept apart, so the pool and its order without them stay as they
        # were and popular titles can't crowd out rare matches ("cyberpunk": Blade Runner for The Matrix) before
        # details tell them apart. A title they also found keeps only what its other inputs give it.
        in_bridged = {c for f in bridged if not any(f is g for g in common_found) for c in f}
        common_only = {c for f in common_found for c in f} - in_bridged - {c for f in (*genre_found, *found) for c in f}
        counts = {c: sum(c in f for f in sims) for c in {c for f in sims for c in f}}
        in_franchise = set(franchise)
        kinds_here = sorted({c[1] for c in merged})
        liked_counts = (
            dict(zip(kinds_here, await asyncio.gather(*(liked_genres(k) for k in kinds_here)), strict=True))
            if liked
            else {}
        )
        fit = {c: _genre_fit(meta[c].get("genre_ids") or [], liked_counts.get(c[1], {}), len(liked)) for c in merged}
        # tv genres are coarser (Action + Adventure are one), so series match more easily than films: measure
        # each title against the best fit in its kind among the discover rows (those are on our services).
        # A kind with no discover rows has nothing to be compared with, so its titles keep the absolute fit.
        found_here = in_genre | in_bridged
        best = {k: max((f for c, f in fit.items() if c[1] == k and c in found_here), default=0.0) for k in kinds_here}

        def score(c: tuple[int, Kind]) -> tuple[bool, bool, float, float]:
            # with a single liked title every similar row scores the same overlap, so genre fit is what
            # separates TMDB's off-target recommendations (and popular bridged rows) from close matches
            overlap = (
                ranking.overlap_weight * counts.get(c, 0)
                + ranking.bridged_weight * (c in in_bridged)
                + ranking.genre_weight * (min(1.0, fit[c] / best[c[1]]) if best[c[1]] else fit[c])
            )
            return (c not in in_genre, c not in in_franchise, -overlap, -_quality(meta[c], ranking))

        merged.sort(key=score)
        if cap:
            # similar isn't limited to our services but bridged/genre discover is, so a pool of pure similar
            # rows can hold nothing that survives the service check; keep a share of the pool for the latter.
            # Franchise titles come on top (FRANCHISE_MAX): a long franchise would fill the pool by itself.
            # Rows found by common keywords only come on top (COMMON_POOL_MAX), so the pool without them stays as
            # it was: they find The Boys for Spider-Man, and can't push Blade Runner ("cyberpunk") out for The
            # Matrix before the final ranking has their keywords.
            rest = [c for c in merged if c not in in_franchise and c not in common_only]
            reserved = set([c for c in rest if c in in_genre or c in in_bridged][: round(cap * ranking.discover_share)])
            others = [c for c in rest if c not in reserved][: cap - len(reserved)]
            common = [c for c in merged if c in common_only and c not in in_franchise][:COMMON_POOL_MAX]
            keep = reserved | set(others) | set(common) | set([c for c in merged if c in in_franchise][:FRANCHISE_MAX])
            merged = [c for c in merged if c in keep]
    if like and signals is not None:
        signals.franchise = set(franchise)
        signals.common_only = common_only
        signals.recommended = {c: sum(c in f for f in sims) for c in {c for f in sims for c in f}}
        kinds_seen = sorted({c[1] for c in merged})
        signals.liked_genres = (
            dict(zip(kinds_seen, await asyncio.gather(*(liked_genres(k) for k in kinds_seen)), strict=True))
            if liked
            else {}
        )
    return merged


# Drama is on most titles, so sharing it says little: in the final "like" ranking it only counts when it is all the
# liked titles have
WEAK_GENRES = frozenset({18})
MATCH_NAMES = 3  # shared keywords a card names (as `matches`), strongest first: what Claude says "why" with


def rank_like(
    cards: list[TitleCard], details: dict[str, Details], signals: LikeSignals, ranking: Ranking
) -> list[TitleCard]:
    """Final order of a "like" search, now that each card's keywords are known (`details` by token): keyword_weight
    x shared keyword weight (relative to the best card), franchise_weight for a title named like the liked one that
    also shares FRANCHISE_SHARED keywords with it, overlap_weight per liked title recommending it,
    final_genre_weight x genre fit (relative to the best of its kind). Ties keep the pool order. Sets each card's
    `matches`."""

    def shared(d: Details) -> list[int]:
        return sorted((k for k in d.keywords if k in signals.weights), key=lambda k: -signals.weights[k])

    kw = {c.token: sum(signals.weights[k] for k in shared(details[c.token])) for c in cards}

    def strong(counts: dict[int, int]) -> dict[int, int]:
        return {g: n for g, n in counts.items() if g not in WEAK_GENRES} or counts

    liked = {k: strong(v) for k, v in signals.liked_genres.items()}
    fit = {
        c.token: _genre_fit(
            [g for g in details[c.token].genre_ids if g in liked.get(c.kind, {}) or g not in WEAK_GENRES],
            liked.get(c.kind, {}),
            signals.n_liked,
        )
        for c in cards
    }
    best_kw = max(kw.values(), default=0.0)
    best_fit = {k: max((fit[c.token] for c in cards if c.kind == k), default=0.0) for k in ("movie", "tv")}

    def score(c: TitleCard) -> float:
        key = (c.tmdb_id, c.kind)
        return (
            # keywords count for less when the genres disagree ("insurgence" doesn't make Step Up 2 like The Matrix)
            ranking.keyword_weight * (kw[c.token] / best_kw if best_kw else 0.0) * (0.5 + 0.5 * rel_fit(c))
            # the name alone isn't enough: "Friends with Benefits" shares one keyword with Friends, its sequels many
            + ranking.franchise_weight
            * (key in signals.franchise and len(shared(details[c.token])) >= FRANCHISE_SHARED)
            # TMDB's recommendations are noisy (Mr. Mercedes for The Expanse): half weight without a shared keyword
            + ranking.overlap_weight * signals.recommended.get(key, 0) * (1.0 if kw[c.token] > 0 else 0.5)
            + ranking.final_genre_weight * rel_fit(c)
        )

    def rel_fit(c: TitleCard) -> float:
        return fit[c.token] / best_fit[c.kind] if best_fit[c.kind] else 0.0

    def weak(c: TitleCard) -> bool:
        # found only because it shares a common keyword ("artificial intelligence (a.i.)" for Atlas and The Matrix):
        # it needs a second shared keyword (The Boys: "superhero" and "based on comic") to rank among the rest
        return (c.tmdb_id, c.kind) in signals.common_only and len(shared(details[c.token])) < COMMON_SHARED

    for c in cards:
        c.matches = [signals.names[k] for k in shared(details[c.token])[:MATCH_NAMES]]
    return sorted(cards, key=lambda c: (weak(c), -score(c)))


SPREAD_WINDOW = 3  # a missing service's title may come from up to this many times `limit` deep in the ranking


def spread_services(cards: list[TitleCard], limit: int, window: int) -> list[TitleCard]:
    """The first `limit` cards, with each service they leave out brought in by its best-ranked title within the first
    `window`, in place of the lowest-ranked card whose services the others still cover (so no service is lost). The
    result stays in rank order. A service with no title in the window, or no card to give up, stays out."""
    top, pool = list(cards[:limit]), list(cards[limit:window])

    def covered(without: int | None = None) -> set[str]:
        return {s for i, c in enumerate(top) if i != without for s in c.services}

    for svc in dict.fromkeys(s for c in cards[:window] for s in c.services):
        if svc in covered():
            continue
        extra = next((c for c in pool if svc in c.services), None)
        if extra is None:
            continue
        spare = next((i for i in reversed(range(len(top))) if set(top[i].services) <= covered(without=i)), None)
        if spare is None:
            continue
        top[spare] = extra
        pool.remove(extra)
    rank = {c.token: i for i, c in enumerate(cards)}
    return sorted(top, key=lambda c: rank[c.token])


async def find_titles(
    source: CatalogSource,
    registry: PickRegistry,
    seen: set[str],
    services: dict[str, Service],
    policy: KidsPolicy,
    *,
    query: str | None = None,
    like: list[str] | None = None,
    service: ServiceId | None = None,
    kind: Kind | None = None,
    kids_mode: bool = False,
    kids_age: int | None = None,
    min_hours: float | None = None,
    max_hours: float | None = None,
    exclude_tmdb_ids: list[int | str] | None = None,
    limit: int = 10,
    concurrency: int = 8,
    candidates: int = 20,
    hours_candidates: int = 40,
    ranking: Ranking = NO_RANKING,
    turn_id: str | None = None,
    question: str | None = None,
    stats: LookupStats | None = None,
    genres: list[str] | None = None,
    genre_aliases: dict[str, list[str]] | None = None,
    genre_keywords: dict[str, list[str]] | None = None,
) -> list[TitleCard]:
    kids_mode = kids_mode or kids_age is not None  # defence in depth: an age always means kids mode
    kids_age = effective_age(kids_age, policy)  # and an age above [kids] max_age counts as max_age
    excluded, excluded_pairs = parse_excludes(exclude_tmdb_ids or [])
    signals = LikeSignals() if like else None
    by_hours = min_hours is not None or max_hours is not None
    # a length filter rejects most titles, so it looks deeper (discover page 2) and at more candidates
    cap = max(hours_candidates if by_hours else candidates, limit)  # never fewer than the caller asked for
    cands = await _candidates(
        source, services, query, like, service, kind, policy, kids_mode, kids_age, stats, genres, genre_aliases,
        limit=limit, pages=HOURS_DISCOVER_PAGES if by_hours else 1, ranking=ranking, exclude=excluded, cap=cap,
        signals=signals, genre_keywords=genre_keywords, exclude_pairs=excluded_pairs,
    )  # fmt: skip
    if signals is not None:  # franchise and common-keyword titles come on top of the cap (_candidates)
        extra = signals.franchise | signals.common_only
        cands = (
            [c for c in cands if c in signals.franchise][:FRANCHISE_MAX]
            + [c for c in cands if c not in extra][:cap]
            + [c for c in cands if c in signals.common_only and c not in signals.franchise][:COMMON_POOL_MAX]
        )
    else:
        cands = cands[:cap]

    sem = asyncio.Semaphore(max(1, concurrency))

    def eligible(d: Details) -> bool:
        on = _matching_services(d, services)
        return (
            not d.adult
            and bool(on)
            and (service is None or service in on)
            and allowed(d.certification, kids_age, kids_mode, policy)
        )

    def in_range(minutes: int, margin: float = 0.0) -> bool:
        if min_hours is not None and minutes < min_hours * 60 * (1 - margin):
            return False
        return max_hours is None or minutes <= max_hours * 60 * (1 + margin)

    def near_range(d: Details) -> bool:
        """Worth an exact lookup: no estimate, or some span's estimate is within the margin of the range."""
        spans = _spans(d)
        return not spans or any(in_range(m, ESTIMATE_MARGIN) for _, m in spans)

    async def fetch(c: tuple[int, Kind]) -> Details | None:
        try:
            async with sem:
                d = await source.details(*c)
            # series lengths are estimates from typical episode runtimes; fetch real season-1 runtimes only
            # for titles that could still pass (movie runtimes are exact already)
            if by_hours and c[1] == "tv" and eligible(d) and near_range(d):
                async with sem:
                    d = await source.details(*c, exact_minutes=True)
        except Exception:
            if stats:
                stats.details_failed += 1
            return None
        if stats:
            stats.details_ok += 1
        return d

    all_details = await asyncio.gather(*(fetch(c) for c in cands))

    cards: list[TitleCard] = []
    card_details: dict[str, Details] = {}
    for (tmdb_id, k), d in zip(cands, all_details, strict=True):
        if d is None:
            continue
        if not eligible(d):  # kids safety: every candidate, whatever produced it
            continue
        on = _matching_services(d, services)
        if service in on:  # the requested service is the pick's service, then config order
            on = [service] + [x for x in on if x != service]
        binge_seasons = binge_minutes = None
        if by_hours:
            if k == "movie":
                if d.runtime_minutes is None or not in_range(d.runtime_minutes):
                    continue
            else:
                match = next(((s, m) for s, m in _spans(d) if in_range(m)), None)
                if match is None:
                    continue
                binge_seasons, binge_minutes = match
        data = dict(
            tmdb_id=tmdb_id,
            kind=k,
            title=d.title,
            year=d.year,
            overview=d.overview,
            poster_url=d.poster_url,
            services=on,
            service=on[0],
            asked_service=service,
            certification=d.certification,
            minutes=d.runtime_minutes if k == "movie" else d.season1_minutes,
            total_minutes=d.total_minutes if k == "tv" else None,
            seasons=d.seasons if k == "tv" else None,
            seen=d.title.lower() in seen,
            binge_seasons=binge_seasons,
            binge_minutes=binge_minutes,
        )
        token = registry.issue(data, kids_mode, kids_age, turn_id=turn_id, question=question)
        cards.append(TitleCard(token=token, **data))
        card_details[token] = d
        if service is not None and len(cards) >= limit and signals is None:
            break
    if signals is not None and (signals.weights or signals.franchise):
        cards = rank_like(cards, card_details, signals, ranking)
    if service is not None:
        return cards[:limit]
    # No service asked: show what each service has (when it ranks close enough), and say which had nothing.
    if stats is not None:
        found = {s for c in cards for s in c.services}
        stats.no_titles_on = [s.id for s in services.values() if s.tmdb_provider_ids and s.region and s.id not in found]
    return spread_services(cards, limit, SPREAD_WINDOW * limit)
