"""Application wiring and the `lumitv` CLI: serve, doctor, import-netflix."""

import argparse
import asyncio
import contextlib
import copy
import hmac
import json
import logging
import re
import sys
import time
from pathlib import Path

import uvicorn
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from . import doctor as doctor_mod, services as services_mod, store as store_mod, stt as stt_mod, warm as warm_mod
from .ask import AskRequest, AskService
from .budget import AskBudget
from .catalog import PickRegistry, load_genre_aliases, load_genre_keywords
from .catalog_source import CatalogSource
from .claude_runner import ClaudeRunner
from .config import Config, load as load_config
from .conversation import Conversations
from .metrics import Metrics
from .models import MAX_VIEWERS, Viewer
from .netflix_import import import_csv
from .stall import StallWatch
from .store import Store
from .title_ids import TitleIdResolver, Wikidata
from .tmdb import Cache, Tmdb
from .tools import INSTRUCTIONS_PATH, build_server, render_tv_instructions
from .tvlink import TvHub, load_hello

STALE_LOG_EVERY_S = 60.0


class BearerAuth:
    """ASGI wrapper: requires `Authorization: Bearer <token>` on HTTP requests."""

    def __init__(self, app, token: str):
        self._app = app
        self._token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            header = dict(scope["headers"]).get(b"authorization", b"")
            scheme, _, supplied = header.partition(b" ")
            if scheme.lower() != b"bearer" or not hmac.compare_digest(supplied.strip(), self._token):
                await JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})(
                    scope, receive, send
                )
                return
        await self._app(scope, receive, send)


AUDIO_TYPES = {"audio/wav", "audio/wave", "audio/x-wav"}
MAX_ASK_BODY = 8 * 1024  # a POST /ask JSON body (a question is at most [ask] max_question_chars)


class _BodyTooLarge(Exception):
    pass


async def _read_body(request: Request, limit: int) -> bytes:
    """The request body, in memory; _BodyTooLarge as soon as it passes `limit` bytes."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _BodyTooLarge
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise _BodyTooLarge
    return bytes(body)


WARMS: set[asyncio.Task] = set()  # running warm-ups (a task needs a reference until it ends)


def _speech_warmer(stt, metrics: Metrics):
    """The TV's `ask_warm` (its mic just opened): run the speech model once while the user is still talking, so a
    model the shared PC paged out is back in memory when the clip arrives. Logged as stt_warm (ms = page-in cost)."""
    warm = getattr(stt, "warm", None)

    async def run(ask_id: str) -> None:
        try:
            took = await warm()
        except Exception:
            logging.getLogger("lumitv").warning("speech warm-up failed", exc_info=True)
            return
        if took is not None:
            metrics.log("stt_warm", ask_id=ask_id, ms=round(took * 1000))

    def on_warm(ask_id: str) -> None:
        if warm is None:
            return
        task = asyncio.get_running_loop().create_task(run(ask_id))
        WARMS.add(task)
        task.add_done_callback(WARMS.discard)

    return on_warm


def create_app(
    cfg: Config,
    source: CatalogSource | None = None,
    transcriber: stt_mod.Transcriber | None = None,
    budget: AskBudget | None = None,
) -> Starlette:
    """`transcriber` replaces the one [speech] configures, `budget` the one [ask] configures (tests: a fake engine,
    an injected clock)."""
    services = services_mod.load(cfg)
    metrics = Metrics(cfg.server.log_path, secrets=[cfg.tmdb_token, cfg.server_token, cfg.claude_oauth_token])
    if any(s.wanted_ids for s in services.values()) and not cfg.open.user_agent_contact.strip():
        metrics.log(
            "setup_incomplete",
            level="WARNING",
            missing="[open] user_agent_contact",
            effect="Wikidata refuses title-id lookups: picks open the app's home screen, not the title",
        )
    tmdb: Tmdb | None = None
    cache: Cache | None = None
    wikidata: Wikidata | None = None
    title_ids: TitleIdResolver | None = None
    if source is None:
        cache = Cache(cfg.server.db_path)
        stale_logged: dict[str, float] = {}

        def on_stale(path: str, err: str) -> None:
            now = time.monotonic()  # at most one warning per path per minute
            if now - stale_logged.get(path, -STALE_LOG_EVERY_S) >= STALE_LOG_EVERY_S:
                stale_logged[path] = now
                metrics.log("tmdb_stale", level="WARNING", path=path, error=err)

        tmdb = source = Tmdb(
            cfg.tmdb_token, cache, cfg.ratings, cfg.timeouts.tmdb_s, on_cache=metrics.cache, on_stale=on_stale,
            ages=cfg.kids.ages, concurrency=cfg.server.lookup_concurrency,
        )  # fmt: skip
        wikidata = Wikidata(
            cache,
            cfg.timeouts.wikidata_s,
            cfg.open.wikidata_ttl_days * 86400,
            cfg.open.wikidata_miss_ttl_days * 86400,
            cfg.open.user_agent_contact,
        )
        title_ids = TitleIdResolver(tmdb, wikidata, services, cfg.open.title_id_wait_s)
    store = Store(cfg.server.db_path)
    hub = TvHub(cfg.server_token, cfg.timeouts.tv_ack_s, load_hello(cfg), services, cfg.open)
    registry = PickRegistry()

    async def on_opened(token: str, service: str | None) -> None:
        pick = registry.get(token)
        metrics.log("tv_opened", turn_id=pick.turn_id if pick else None, service=service)
        if pick and pick.services:
            opened = service if service in pick.services else pick.service or pick.services[0]  # its primary service
            store.add_opened(pick.tmdb_id, pick.kind, pick.title, opened)

    async def on_rendered(answer_id: str, ms: int, phases: dict[str, int]) -> None:
        turn_id = hub.turn_for(answer_id)
        if turn_id:
            metrics.record_turn(turn_id, "render", ms)
        metrics.log("tv_rendered", turn_id=turn_id, ms=ms, **{f"{k}_ms": v for k, v in phases.items()})

    hub.on_opened = on_opened
    hub.services_used = lambda p: store.services_used(p.tmdb_id, p.kind, p.title)
    hub.services_bad = lambda p: store.bad_services(p.tmdb_id, p.kind)
    hub.viewers = lambda: [v.model_dump() for v in store.viewers()]

    def on_viewers_set(raw: list) -> list[dict]:
        """The TV saved who watches here: stored (household tables, backed up), echoed back as saved."""
        saved = store.set_viewers([Viewer.model_validate(v) for v in raw[:MAX_VIEWERS]])
        metrics.log("viewers_set", count=len(saved), children=sum(v.is_child for v in saved))  # never names
        return [v.model_dump() for v in saved]

    hub.on_viewers_set = on_viewers_set

    def on_outcome(token: str, service: str, outcome: str) -> None:
        pick = registry.get(token)
        metrics.log("tv_open_outcome", turn_id=pick.turn_id if pick else None, service=service, outcome=outcome)
        if pick and service in pick.services:
            store.add_outcome(pick.tmdb_id, pick.kind, service, outcome)  # type: ignore[arg-type]

    hub.on_outcome = on_outcome
    hub.on_event = metrics.log
    hub.on_rendered = on_rendered

    runner: ClaudeRunner | None = None
    asks: AskService | None = None
    budget = budget or AskBudget(cfg.ask.per_minute, cfg.ask.per_day)

    def over_budget() -> JSONResponse | None:
        """429 when one more ask would pass [ask] per_minute or per_day; the TV shows its "limit" line. Nothing is
        counted here: an ask counts (`budget.charge()`) once `submit` scheduled it, so a repeated or early-cancelled
        ask, like a refused one, costs no quota."""
        verdict = budget.check()
        if verdict == "ok":
            return None
        metrics.log("ask_limited", level="WARNING", cap=verdict, used_today=budget.used_today)
        return JSONResponse({"error": "limit", "cap": verdict}, status_code=429)

    ask_kw: dict = {}
    if cfg.ask.enabled:
        runner = ClaudeRunner(cfg, render_tv_instructions(cfg, services))
        conversations = Conversations(cfg.ask.new_conversation_after_s, new_session_id=runner.fresh_session_id)
        if transcriber is None:
            transcriber = stt_mod.make_transcriber(cfg.speech)
        asks = AskService(
            hub, runner, conversations, metrics, cfg.speech.ignore_phrases, transcriber=transcriber,
            stt_timeout_s=cfg.speech.timeout_s, max_audio_s=cfg.speech.max_audio_s,
            max_question=cfg.ask.max_question_chars, household=store.viewers,
        )  # fmt: skip
        hub.on_ask_timing = asks.on_timing
        stt = transcriber
        # the TV sends the remote's audio here only while the model is loaded; while it loads (10-25 s after a start)
        # or after a failed load the TV's own recognizer answers instead. An engine without `ready` (tests) is loaded.
        hub.speech = lambda: asks.hears_audio and getattr(stt, "ready", True)
        if isinstance(stt, stt_mod.WhisperTranscriber):
            stt.on_ready = hub.announce  # a TV that connected during the load hears that speech is on now
        hub.on_ask_cancel = asks.cancel
        hub.on_ask_warm = _speech_warmer(stt, metrics)
        ask_kw = {"asks": asks, "ask_id_of": lambda ctx: runner.ask_id_of(ctx.headers), "watching": asks.watching}

    mcp = build_server(cfg, services, source, store, hub, registry, metrics, title_ids, **ask_kw)
    mcp_app = mcp.streamable_http_app(host=cfg.server.host)

    async def close_asks() -> None:
        """Stop the running ask and every Claude Code process (idempotent; see LumiServer)."""
        if asks:
            await asks.close()
        if runner:
            await runner.close()

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        # a stalled event loop delays the TV link, tools and uploads all at once: log it, with where it was stuck
        stalls = StallWatch(lambda ms, where: metrics.log("loop_stall", level="WARNING", ms=ms, where=where))
        stalls.start()
        warm_task = None
        if tmdb:  # background: startup never waits for it
            warm_task = asyncio.create_task(
                warm_mod.run_schedule(
                    tmdb,
                    services,
                    cfg.cache.warm_at,
                    max(1, cfg.server.lookup_concurrency // 2),
                    metrics,
                    cfg.cache.warm_genres,
                    load_genre_aliases(INSTRUCTIONS_PATH.parent, cfg.language.code),
                    title_ids,
                    min_votes=cfg.ranking.min_votes or None,
                    genre_keywords=load_genre_keywords(INSTRUCTIONS_PATH.parent, cfg.language.code),
                    daily=daily,
                    kids_policy=cfg.kids_policy(),
                    kids_min_votes=cfg.ranking.kids_min_votes or None,
                )
            )
        if runner:  # pre-warm Claude Code for the first ask; never raises, never waits for the process
            cfg.ask.workdir.mkdir(parents=True, exist_ok=True)
            await runner.start()
            if isinstance(transcriber, stt_mod.WhisperTranscriber):  # loads in a thread; startup never waits
                transcriber.start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await stalls.stop()
            await close_asks()
            if isinstance(transcriber, stt_mod.WhisperTranscriber):
                await transcriber.close()
            if warm_task:
                warm_task.cancel()
                await asyncio.gather(warm_task, return_exceptions=True)
            if tmdb:
                await tmdb.aclose()
            if wikidata:
                await wikidata.aclose()
            if cache:
                cache.close()
            store.close()
            metrics.close()

    def daily() -> None:
        """After each warm: back up the household tables (backups/ next to the database, newest 14), prune the cache."""
        path = store_mod.backup(cfg.server.db_path, Path(cfg.server.db_path).parent / "backups")
        pruned = cache.prune() if cache else 0
        metrics.log("daily_done", backup=path.name, cache_pruned=pruned)

    async def health(_request):
        return PlainTextResponse("ok")

    async def ask_endpoint(scope, receive, send):
        """POST /ask: an ask from the TV. 202 at once; the answer reaches the TV over its link."""
        if asks is None:
            response = JSONResponse({"error": "disabled"}, status_code=503)
        else:
            try:
                body = json.loads(await _read_body(Request(scope, receive), MAX_ASK_BODY))
                if not isinstance(body, dict):
                    raise ValueError("not an object")
            except _BodyTooLarge:
                response = JSONResponse({"error": "too large"}, status_code=413)
            except ValueError:  # includes json.JSONDecodeError and bad UTF-8
                response = JSONResponse({"error": "invalid json"}, status_code=400)
            except ClientDisconnect:
                return
            else:
                try:
                    req = AskRequest.model_validate(body)
                except ValidationError as e:
                    first = e.errors()[0]
                    name = ".".join(str(x) for x in first["loc"]) or None
                    response = JSONResponse({"error": "invalid", "field": name}, status_code=400)
                else:
                    if asks.too_long(req):
                        response = JSONResponse({"error": "invalid", "field": "text"}, status_code=400)
                    elif (limited := over_budget()) is not None:
                        response = limited
                    else:
                        if asks.submit(req):
                            budget.charge()
                        response = JSONResponse({"ask_id": req.ask_id}, status_code=202)
        await response(scope, receive, send)

    async def ask_audio_endpoint(scope, receive, send):
        """POST /ask/audio: a spoken ask (WAV body). Checked, then 202 at once; it is transcribed afterwards and the
        answer reaches the TV over its link. The audio is held in memory only, never written or logged."""
        request = Request(scope, receive)
        params = request.query_params
        if asks is None:
            response = JSONResponse({"error": "disabled"}, status_code=503)
        elif not asks.hears_audio:
            response = JSONResponse({"error": "no speech-to-text"}, status_code=503)
        elif request.headers.get("content-type", "").split(";")[0].strip().lower() not in AUDIO_TYPES:
            response = JSONResponse({"error": "not audio/wav"}, status_code=415)
        elif (limited := over_budget()) is not None:  # before the body is read: a refused upload costs no bandwidth
            response = limited
        else:
            try:
                t0 = time.monotonic()
                wav = await _read_body(request, stt_mod.MAX_AUDIO_BYTES)
                scheduled = asks.submit_audio(
                    params.get("ask_id", ""), wav, params.get("answer_id"), params.get("focused_token"),
                    viewers=[v.strip() for v in params.get("viewers", "").split(",") if v.strip()][:MAX_VIEWERS],
                    upload_ms=(time.monotonic() - t0) * 1000,  # receiving the body: slow network or a stalled server
                )  # fmt: skip
            except ClientDisconnect:  # the TV gave up mid-upload; nobody to answer
                return
            except _BodyTooLarge:
                response = JSONResponse({"error": "too large"}, status_code=413)
            except ValidationError as e:
                name = ".".join(str(x) for x in e.errors()[0]["loc"]) or None
                response = JSONResponse({"error": "invalid", "field": name}, status_code=400)
            except stt_mod.AudioTooLong:
                response = JSONResponse({"error": "too long"}, status_code=413)
            except ValueError:
                response = JSONResponse({"error": "not 16 kHz mono 16-bit wav"}, status_code=415)
            else:
                if scheduled:
                    budget.charge()
                response = JSONResponse({"ask_id": params["ask_id"]}, status_code=202)
        await response(scope, receive, send)

    async def stats_app(scope, receive, send):
        body = {**metrics.stats(), "tv": vars(hub.state), "cache_rows": cache.count() if cache else None}
        if asks is not None:  # how the last ask ended: doctor reports a logged-out, limited or missing Claude Code
            body["ask"] = {**body.get("ask", {}), "last": asks.last_outcome()}
        await JSONResponse(body)(scope, receive, send)

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/stats", BearerAuth(stats_app, cfg.server_token), methods=["GET"]),
            Route("/ask", BearerAuth(ask_endpoint, cfg.server_token), methods=["POST"]),
            Route("/ask/audio", BearerAuth(ask_audio_endpoint, cfg.server_token), methods=["POST"]),
            hub.websocket_route,  # /tv checks its own token
            Mount("/", app=BearerAuth(mcp_app, cfg.server_token)),
        ],
        lifespan=lifespan,
    )
    app.state.close_asks = close_asks
    return app


class LumiServer(uvicorn.Server):
    """Stops TV asks first when shutting down. Their warm Claude Code processes hold MCP connections open, and uvicorn
    waits for open connections to close before it runs the lifespan shutdown (even a forced quit waits on Python
    3.12+), so without this the server never exits."""

    async def shutdown(self, sockets=None) -> None:
        close = getattr(getattr(self.config.app, "state", None), "close_asks", None)
        if close is not None:
            try:
                await close()
            except Exception:
                logging.getLogger("lumitv").exception("stopping asks at shutdown failed")
        await super().shutdown(sockets)


_TOKEN_PARAM = re.compile(r"(token=)[^\s&\"']+")


class RedactToken(logging.Filter):
    """Defence in depth for log lines that quote a URL (uvicorn's WebSocket accept): no route takes the token in a
    URL (/tv refuses one in the query string), but should a client ever send `?token=`, the line keeps `token=***`."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _TOKEN_PARAM.sub(r"\1***", message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def make_uvicorn_config(app, cfg: Config) -> uvicorn.Config:
    """uvicorn's logging plus this server's own: `lumitv.*` from INFO up on stderr with a timestamp and the logger
    name (the JSONL file has the structured record; these lines say why: process starts and exits, stderr tails),
    through the same token redaction. Libraries log from WARNING up the same way (MCPServer's basicConfig would
    otherwise print bare INFO messages); httpx is kept to warnings, as its INFO line per request quotes the URL,
    search words included."""
    log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    log_config["filters"] = {"redact_token": {"()": RedactToken}}
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        log_config["loggers"].setdefault(name, {}).setdefault("filters", []).append("redact_token")
    log_config["formatters"]["lumitv"] = {"format": LOG_FORMAT}
    log_config["handlers"]["lumitv"] = {
        "class": "logging.StreamHandler", "formatter": "lumitv", "stream": "ext://sys.stderr",
        "filters": ["redact_token"],
    }  # fmt: skip
    log_config["root"] = {"handlers": ["lumitv"], "level": "WARNING"}
    log_config["loggers"]["lumitv"] = {"level": "INFO"}
    for name in ("httpx", "httpx2"):
        log_config["loggers"][name] = {"level": "WARNING"}
    # loop="asyncio" without reload/workers: uvicorn then builds a Proactor loop on Windows, which asks need to
    # start Claude Code (asyncio subprocesses); the Selector loop it uses with reload/workers can't run them
    return uvicorn.Config(
        app, host=cfg.server.host, port=cfg.server.port, access_log=False, log_config=log_config, loop="asyncio"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumitv")
    parser.add_argument("--config", default="lumitv.toml", type=Path)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the MCP + TV server")
    sub.add_parser("doctor", help="check the running server against the performance budgets")
    imp = sub.add_parser("import-netflix", help="import a Netflix viewing-activity CSV")
    imp.add_argument("csv", type=Path)
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (ValueError, OSError) as e:
        print(e, file=sys.stderr)
        return 2
    if args.cmd == "doctor":
        return doctor_mod.run(cfg)
    if args.cmd == "import-netflix":
        added = import_csv(Store(cfg.server.db_path), args.csv)
        print(f"Added {added} rows")
        return 0
    try:
        app = create_app(cfg)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    LumiServer(make_uvicorn_config(app, cfg)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
