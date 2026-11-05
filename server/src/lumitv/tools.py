"""MCP tools Claude talks to (Claude Desktop, and Claude Code for asks made on the TV): find_titles, show_on_tv,
open_title, record_feedback, taste_profile."""

import functools
import logging
import re
import time
import unicodedata
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import catalog
from .catalog import LookupStats, Pick, PickRegistry
from .catalog_source import CatalogSource
from .config import Config
from .kids import allowed, effective_age
from .metrics import Metrics
from .models import Kind, TitleCard, Viewer
from .services import Service
from .store import Store
from .title_ids import TitleIdResolver
from .tvlink import OpenResult, ShowResult, length_label

INSTRUCTIONS_PATH = Path(__file__).resolve().parents[2] / "content" / "instructions.md"
TV_INSTRUCTIONS_PATH = INSTRUCTIONS_PATH.parent / "instructions_tv.md"
MAX_FIND_QUESTION, MAX_HEADLINE, MAX_SUBLINE = 90, 70, 140
MAX_TASTE, MAX_OVERVIEW = 400, 160
MAX_REASON = 48  # one line under a poster: about two short lines at the poster's width
MAX_REPLY = 140  # a chat reply shown on the TV as an ask_reply line (ask.py clips to it)
MAX_TITLE = 120  # a title or a `matches` keyword handed to Claude (TMDB data; a few titles run long)
MAX_FEEDBACK_TITLE, MAX_FEEDBACK_NOTE = 200, 500  # record_feedback's free text, as stored

log = logging.getLogger("lumitv.tools")

_call_info: ContextVar[dict] = ContextVar("lumitv_call_info")


def _note(**fields) -> None:
    """Attach fields to the tool call being logged (no-op outside one)."""
    info = _call_info.get(None)
    if info is not None:
        info.update(fields)


class Hub(Protocol):
    def looking(self, turn_id: str, question: str, ask_id: str | None = None) -> None: ...

    def preview(self, ask_id: str, cards: list[TitleCard]) -> None: ...

    async def show(
        self,
        question: str,
        headline: str,
        subline: str,
        picks: list[Pick],
        ids: dict[str, dict[str, str]] | None = None,
        ask_id: str | None = None,
        reasons: dict[str, str] | None = None,
    ) -> ShowResult: ...

    async def open(self, pick: Pick, ids: dict[str, str] | None = None, service: str | None = None) -> OpenResult: ...

    def vpn_warning(self, pick: Pick, service: str | None = None) -> str | None: ...

    def providers(self, pick: Pick, ids: dict[str, str] | None) -> list[str]: ...


class AskHooks(Protocol):
    """Progress of a TV ask's tool calls (ask.AskService)."""

    def mark(
        self, ask_id: str, event: Literal["find_start", "find_end", "show_start", "show_end", "opened"]
    ) -> None: ...


def _placeholders(cfg: Config, services: dict[str, Service]) -> dict[str, str]:
    """What the instruction files may say with braces: config and the caps the tools enforce, so a changed cap can't
    leave the prose behind."""
    with_providers = [s for s in services.values() if s.tmdb_provider_ids]
    on = [f"{s.label} needs the TV's VPN on to work" for s in services.values() if s.vpn == "on"]
    off = [f"{s.label} works best with it off" for s in services.values() if s.vpn == "off"]
    vpn_notes = "; ".join(on + off) + "." if on or off else ""
    return {
        "{assistant_name}": cfg.assistant.name,
        "{services}": ", ".join(s.label for s in with_providers),
        "{picks}": str(cfg.answers.picks),
        "{headline_max}": str(MAX_HEADLINE),
        "{reason_max}": str(MAX_REASON),
        "{reply_max}": str(MAX_REPLY),
        "{vpn_notes}": vpn_notes,
    }


def _render(path: Path, values: dict[str, str]) -> str:
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace(key, value)
    return text.strip()


def render_instructions(cfg: Config, services: dict[str, Service], path: Path = INSTRUCTIONS_PATH) -> str:
    """Fill the templated conversation instructions from config; the file names no service or region."""
    return _render(path, _placeholders(cfg, services))


def render_tv_instructions(cfg: Config, services: dict[str, Service], path: Path = TV_INSTRUCTIONS_PATH) -> str:
    """Instructions for asks made on the TV itself (`claude -p`): the conversation instructions plus the TV rules."""
    values = _placeholders(cfg, services)
    return _render(INSTRUCTIONS_PATH, values) + "\n\n" + _render(path, values)


_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")


def untrusted(text: str | None) -> str:
    """Text from TMDB/Wikidata (titles, overviews, keywords) as plain data for Claude: no control, invisible or
    direction-changing characters (they can hide text), no links, single spaces. Instructions hidden in a title or
    overview are still words, so the instructions also tell Claude that tool results are data."""
    if not text:
        return ""
    kept = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" or ch in "\n\t")
    return " ".join(_URL.sub("", kept).split())


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def taste_summary(profile: dict, limit: int = MAX_TASTE) -> str:
    parts = []
    if profile["loved"]:
        parts.append("Loved: " + ", ".join(profile["loved"]))
    if profile["disliked"]:
        parts.append("Disliked: " + ", ".join(profile["disliked"]))
    if profile["most_watched"]:
        parts.append("Watch most: " + ", ".join(m["title"] for m in profile["most_watched"][:10]))
    if profile["recently_opened"]:
        parts.append("Recently opened: " + ", ".join(profile["recently_opened"][:5]))
    return _clip(". ".join(parts), limit)


def build_server(
    cfg: Config,
    services: dict[str, Service],
    source: CatalogSource,
    store: Store,
    hub: Hub,
    registry: PickRegistry | None = None,
    metrics: Metrics | None = None,
    title_ids: TitleIdResolver | None = None,
    asks: AskHooks | None = None,
    ask_id_of: Callable[[Context], str | None] = lambda ctx: None,
    now: Callable[[], datetime] = datetime.now,
    watching: Callable[[str | None], list[Viewer]] = lambda ask_id: [],
) -> MCPServer:
    """`title_ids` resolves picks' ids for exact-title launches (Google TV page, Netflix title link); None: picks
    open without those steps. `ask_id_of`: the TV ask a tool call belongs to (from its request), None for Claude
    Desktop; such calls carry the ask id to the TV and report their progress to `asks`. `now`: the local time, for
    the kids-mode default ([kids] default_from/default_until). `watching`: who is watching for a TV ask (the
    youngest child among them sets the kids limit, whatever Claude passes)."""
    registry = registry or PickRegistry()
    metrics = metrics or Metrics(None)
    policy = cfg.kids_policy()
    max_picks = cfg.answers.picks
    mcp = MCPServer(cfg.assistant.name, instructions=render_instructions(cfg, services))
    service_ids = ", ".join(s.id for s in services.values() if s.tmdb_provider_ids)  # the ones with a catalogue
    genre_aliases = catalog.load_genre_aliases(INSTRUCTIONS_PATH.parent, cfg.language.code)
    genre_keywords = catalog.load_genre_keywords(INSTRUCTIONS_PATH.parent, cfg.language.code)
    # (kids_mode, kids_age) of each caller's most recent find_titles: show_on_tv refuses picks from a search with
    # other kids settings, so "show the first ones again" in a kids conversation can't bring back a non-kids answer.
    # Keyed by caller ("tv": asks from the TV, one conversation at a time; "desktop": Claude Desktop), so the two
    # don't trip each other. Two open Desktop conversations still share one key: that only makes show/open ask for a
    # fresh find_titles (it fails safe), and the MCP session id is not exposed to tool code cheaply.
    last_search: dict[str, tuple[bool, int | None]] = {}

    def caller(ask_id: str | None) -> str:
        return "tv" if ask_id else "desktop"

    def require_current_kids_settings(pick: Pick, what: str, ask_id: str | None) -> None:
        key = caller(ask_id)
        if key in last_search and (pick.kids_mode, pick.kids_age) != last_search[key]:
            raise ToolError(f"{what} from an earlier search with different kids settings; call find_titles again")

    def ask_of(ctx: Context) -> str | None:
        """The TV ask this call belongs to (noted in its log line); None (as for Claude Desktop) when it can't be
        told."""
        try:
            ask_id = ask_id_of(ctx)
        except Exception:
            log.exception("could not tell the ask of a tool call")
            return None
        if ask_id:
            _note(ask_id=ask_id)
        return ask_id or None

    def mark(ask_id: str | None, event) -> None:
        if ask_id and asks is not None:
            asks.mark(ask_id, event)

    def logged(stage: str | None = None):
        """One log line per tool call: tool, turn_id (once known), ms, status, plus whatever _note added."""

        def deco(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                info: dict = {}
                reset = _call_info.set(info)
                started = time.perf_counter()
                status = "error"
                cache = {"hits": 0, "misses": 0}
                try:
                    with metrics.track_call() as cache:
                        result = await fn(*args, **kwargs)
                    status = "ok"
                    return result
                except Exception as e:
                    info["error"] = str(e)[:200]
                    raise
                finally:
                    _call_info.reset(reset)
                    ms = round((time.perf_counter() - started) * 1000, 1)
                    turn_id = info.pop("turn_id", None)
                    if status == "ok" and stage and turn_id:
                        metrics.record_turn(turn_id, stage, ms)  # type: ignore[arg-type]
                    metrics.log(
                        "tool_call", tool=fn.__name__, turn_id=turn_id, ms=ms, status=status,
                        cache_hits=cache["hits"], cache_misses=cache["misses"], **info,
                    )  # fmt: skip

            return wrapper

        return deco

    def card_json(c: TitleCard) -> dict:
        out = {
            "token": c.token,
            "tmdb_id": c.tmdb_id,
            "kind": c.kind,
            "title": _clip(untrusted(c.title), MAX_TITLE),
            "year": c.year,
            "services": [services[s].label for s in c.services],
            "certification": c.certification,
            "length": length_label(c),
            "seen": c.seen,
            "overview": _clip(untrusted(c.overview), MAX_OVERVIEW),
        }
        if c.matches:  # "like X": what it shares with X, for the reason line
            out["matches"] = [_clip(untrusted(m), MAX_TITLE) for m in c.matches]
        if c.binge_minutes is not None:  # hours were asked for: which seasons fit (None = the whole run)
            out["binge_seasons"] = c.binge_seasons
            out["binge_minutes"] = c.binge_minutes
        return out

    @mcp.tool(
        description=(
            "Find candidate titles on the family's streaming services. Call first in every turn. "
            f"`question` is the user's latest message (max {MAX_FIND_QUESTION} chars); "
            "it is shown on the TV while you think. "
            "Returns family_taste (what the family loved, disliked, watches and opened), titles (token, tmdb_id, kind, "
            "title, year, services, certification, length, seen, overview; with `like` also matches; with hours also "
            "binge_seasons and binge_minutes), unknown_genres, skipped_kinds, no_titles_on, kids_mode, "
            "kids_mode_default, watching, and degraded when a lookup failed. Use `query` only for a specific "
            "title, `genres` (e.g. comedy, animation, sci-fi, kids, documentary) for types and moods; genres are "
            "combined with AND, so use one or two genres (more narrows results); skipped_kinds lists kinds left out "
            "because they have no match for a requested genre, "
            "`like` for 'similar to these titles' (works across kinds: liked films with kind tv give series in "
            "their genres), `service` to restrict to one service "
            f"(one of: {service_ids}), min_hours/max_hours for length (series: the whole run, or else the first one "
            "or two seasons; each title's `length` and binge_seasons say which), "
            'exclude_tmdb_ids for titles already suggested or rejected, as "movie:<tmdb_id>" / "tv:<tmdb_id>" '
            "(a bare id excludes both kinds, since film and series ids overlap), `limit` (default 6, at most 20) for "
            "how many titles to return. "
            "With no service given, titles cover every service that has a good match, and no_titles_on names the "
            "services with none. "
            "kids_age turns kids mode on (passing it is enough). Leave kids_mode out unless the user said who it is "
            "for: true for a child, false for grown-ups; left out, it follows the household's hours (the result's "
            "kids_mode says what applied and kids_mode_default whether the hours decided it). With `like`, titles "
            "come closest first and carry "
            "`matches`: the keywords they share with the liked title."
        )
    )
    @logged("find")
    async def find_titles(
        ctx: Context,
        question: str,
        query: str | None = None,
        like: list[str] | None = None,
        genres: list[str] | None = None,
        service: str | None = None,
        kind: Kind | None = None,
        kids_mode: bool | None = None,
        kids_age: int | None = None,
        min_hours: float | None = None,
        max_hours: float | None = None,
        exclude_tmdb_ids: list[int | str] | None = None,
        limit: int = 6,
    ) -> dict:
        if service is not None and service not in services:
            raise ToolError(f"Unknown service {service!r}; use one of: {service_ids}")
        if service is not None and not services[service].tmdb_provider_ids:
            raise ToolError(f"{services[service].label} has no catalogue to search; use one of: {service_ids}")
        if kids_age is not None and kids_age < 0:
            raise ToolError("kids_age can't be negative")
        try:
            catalog.parse_excludes(exclude_tmdb_ids or [])
        except ValueError as e:
            raise ToolError(str(e)) from None
        ask_id = ask_of(ctx)
        viewers = watching(ask_id) if ask_id else []
        children = [v.age for v in viewers if v.is_child and v.age is not None]
        defaulted = kids_mode is None and kids_age is None and not viewers
        if children:  # a child is watching: the youngest sets the limit, and nothing Claude passes lifts it
            kids_mode, kids_age = True, min([*children, *([kids_age] if kids_age is not None else [])])
        elif kids_age is not None:  # an age means a child is watching: never filter-free
            kids_mode = True
        elif kids_mode is None:  # grown-ups only when the TV says who is watching; else the household's hours
            kids_mode = False if viewers else cfg.kids.default_on(now())
        kids_age = effective_age(kids_age, policy)  # above [kids] max_age counts as max_age: never 16+/18+ picks
        last_search[caller(ask_id)] = (kids_mode, kids_age)
        question = _clip(question.strip(), MAX_FIND_QUESTION)
        turn_id = uuid.uuid4().hex
        mark(ask_id, "find_start")
        _note(
            turn_id=turn_id, question=question, kids_mode=kids_mode, kids_default=defaulted, service=service, kind=kind
        )
        hub.looking(turn_id, question, ask_id=ask_id)
        lookup = LookupStats()
        cards = await catalog.find_titles(
            source, registry, store.seen_titles(), services, policy,
            query=query, like=like, genres=genres, genre_aliases=genre_aliases, genre_keywords=genre_keywords,
            service=service, kind=kind, kids_mode=kids_mode, kids_age=kids_age,
            min_hours=min_hours, max_hours=max_hours, exclude_tmdb_ids=exclude_tmdb_ids,
            limit=max(1, min(limit, 20)), concurrency=cfg.server.lookup_concurrency, candidates=cfg.answers.candidates,
            hours_candidates=cfg.answers.hours_candidates,
            ranking=cfg.ranking, turn_id=turn_id, question=question, stats=lookup,
        )  # fmt: skip
        degraded = lookup.degraded()
        _note(inputs_failed=lookup.inputs_failed, details_failed=lookup.details_failed, degraded=degraded or None)
        if not cards and "tmdb_unavailable" in degraded:  # an outage must not read as "nothing matched"
            raise ToolError(
                "The film catalogue (TMDB) can't be reached right now; say so and suggest trying again in a minute"
            )
        # a title whose every service lately failed to open or play it is probably listed wrongly: leave it out
        bad = store.bad_services_for((c.tmdb_id, c.kind) for c in cards)
        listed_wrong = [c for c in cards if c.services and set(c.services) <= bad.get((c.tmdb_id, c.kind), set())]
        cards = [c for c in cards if c not in listed_wrong]
        _note(count=len(cards), listed_wrong=len(listed_wrong))
        if title_ids:  # ids for exact-title launches, looked up while Claude picks; show_on_tv then finds them ready
            title_ids.prefetch(cards)
        if ask_id and cards:  # the TV shows the likely posters while Claude chooses (the answer replaces them)
            hub.preview(ask_id, cards[:max_picks])
        mark(ask_id, "find_end")
        out = {
            "family_taste": taste_summary(store.taste_profile(viewers=[v.name for v in viewers] or None)),
            "titles": [card_json(c) for c in cards],
            "unknown_genres": lookup.unknown_genres,
            "skipped_kinds": lookup.skipped_kinds,
            "no_titles_on": [services[s].label for s in lookup.no_titles_on],
            "kids_mode": kids_mode,
            "kids_mode_default": defaulted and kids_mode,
            "watching": [v.label() for v in viewers],
        }
        if degraded:  # only when something failed, so a clean answer carries no noise
            out["degraded"] = degraded
        return out

    @mcp.tool(
        description=(
            f"Show up to {max_picks} picks from find_titles on the TV. headline (max {MAX_HEADLINE} chars) is "
            f"your reply line; subline (max {MAX_SUBLINE}) adds detail; reasons (optional, one per token, in the same "
            f"order, max {MAX_REASON} chars each) says why each pick, shown under its poster. Returns status: screen, "
            "notice (something is playing; the TV shows a small 'answer ready' notice), tv_off or offline."
        )
    )
    @logged("show")
    async def show_on_tv(
        ctx: Context, headline: str, subline: str, tokens: list[str], reasons: list[str] | None = None
    ) -> dict:
        ask_id = ask_of(ctx)
        mark(ask_id, "show_start")
        if len(headline) > MAX_HEADLINE:
            raise ToolError(f"headline too long: max {MAX_HEADLINE} characters")
        if len(subline) > MAX_SUBLINE:
            raise ToolError(f"subline too long: max {MAX_SUBLINE} characters")
        if not 1 <= len(tokens) <= max_picks:
            raise ToolError(f"Show between 1 and {max_picks} picks")
        if reasons is not None and len(reasons) != len(tokens):
            raise ToolError("Give one reason per token, in the same order (or no reasons)")
        # Clipped rather than refused: a refusal costs Claude another round trip for a line under a poster.
        # strict=False: with no reasons there is nothing to pair (a given list was matched to tokens above)
        why = {t: _clip(r.strip(), MAX_REASON) for t, r in zip(tokens, reasons or [], strict=False) if r.strip()}
        picks: list[Pick] = []
        for t in dict.fromkeys(tokens):
            p = registry.get(t)
            if p is None:
                raise ToolError("Unknown pick; call find_titles again")
            picks.append(p)
        if len({(p.kids_mode, p.kids_age) for p in picks}) > 1:
            raise ToolError("Picks mix kids and non-kids searches; call find_titles again")
        require_current_kids_settings(picks[0], "These picks come", ask_id)
        for p in picks:
            if not allowed(p.certification, p.kids_age, p.kids_mode, policy):
                raise ToolError(f"{p.title} is not allowed in kids mode")
        newest = max(picks, key=lambda p: p.seq)
        question = newest.question or ""
        # what the family saw and why, so answers can be judged later (scripts/asks_report.py --picks)
        _note(
            turn_id=newest.turn_id,
            count=len(picks),
            headline=headline,
            subline=subline,
            picks=[{"title": p.title, "service": p.primary_service, "reason": why.get(p.token)} for p in picks],
        )
        ids = await title_ids.for_picks(picks) if title_ids else {}
        _note(title_ids=len(ids))
        status = await hub.show(question, headline, subline, picks, ids, ask_id=ask_id, reasons=why)
        mark(ask_id, "show_end")
        _note(result=status)
        return {"status": status, "shown": [p.title for p in picks]}

    @mcp.tool(
        description=(
            "Open the title the user chose on the TV (its page or the app's search, never autoplay). Takes a token "
            "from find_titles, and optionally `service` (one of the title's services, by id or label, or a part of "
            'the name that fits only one) when the user names one ("open it on Prime"); without it the best '
            "service opens. Returns status: sent, tv_off or "
            "offline, and the service it opened in. When sent it may also carry vpn_warning (the TV's VPN state is "
            "wrong for the service; the title still opened, the TV shows a warning)."
        )
    )
    @logged()
    async def open_title(ctx: Context, token: str, service: str | None = None) -> dict:
        ask_id = ask_of(ctx)
        pick = registry.get(token)
        if pick is None:
            raise ToolError("Unknown pick; call find_titles again")
        require_current_kids_settings(pick, f"{pick.title} comes", ask_id)
        if not allowed(pick.certification, pick.kids_age, pick.kids_mode, policy):
            raise ToolError(f"{pick.title} is not allowed in kids mode")
        if not pick.services:
            raise ToolError(f"{pick.title} is not on any configured service")
        chosen = None
        if service:
            # the id or label as given, else a part of one name only ("video" fits Prime Video and Videoland: ask)
            wanted = service.casefold().strip()
            names = {s: (s.casefold(), services[s].label.casefold()) for s in pick.services}
            exact = [s for s, known in names.items() if wanted in known]
            partial = [s for s, known in names.items() if any(wanted in k for k in known)]
            matches = exact or partial
            if len(matches) > 1:
                raise ToolError(f"{service!r} could be {' or '.join(services[s].label for s in matches)}; say which")
            if not matches:
                on = ", ".join(services[s].label for s in pick.services)
                raise ToolError(f"{pick.title} isn't on {service}; it is on {on}")
            chosen = matches[0]
        ids = (await title_ids.for_picks([pick])).get(pick.token, {}) if title_ids else {}
        _note(title_ids=sorted(ids))
        chosen = chosen or hub.providers(pick, ids)[0]
        _note(service=chosen)
        opened_by = None
        if ask_id:
            try:
                opened_by = [v.name for v in watching(ask_id)]
            except Exception:  # bookkeeping only; the kids rule above used the pick's own settings
                _note(opened_by="unknown")
        status = await hub.open(pick, ids, service=chosen)
        out: dict = {"status": status}
        if status == "sent":
            mark(ask_id, "opened")
            store.add_opened(pick.tmdb_id, pick.kind, pick.title, chosen, opened_by)
            out["service"] = services[chosen].label
            warning = hub.vpn_warning(pick, chosen)
            if warning:
                out["vpn_warning"] = warning
        return out

    @mcp.tool(
        description=(
            "Record how the family felt about a title: loved, liked or disliked. Takes a token or a plain title."
        )
    )
    @logged()
    async def record_feedback(
        token_or_title: str, verdict: Literal["loved", "liked", "disliked"], note: str | None = None
    ) -> dict:
        note = _clip(note.strip(), MAX_FEEDBACK_NOTE) if note and note.strip() else None  # stored as given, bounded
        pick = registry.get(token_or_title)
        if pick:
            store.add_feedback(pick.tmdb_id, pick.kind, pick.title, verdict, note)
            return {"status": "recorded", "title": pick.title}
        title = _clip(token_or_title.strip(), MAX_FEEDBACK_TITLE)
        if not title:
            raise ToolError("Give a token from find_titles or a title")
        store.add_feedback(None, None, title, verdict, note)
        return {"status": "recorded", "title": title}

    @mcp.tool(
        description="The family's taste profile. Only for questions like 'what do we like?'; not part of a normal turn."
    )
    @logged()
    async def taste_profile() -> dict:
        return store.taste_profile()

    return mcp
