import os
import re
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .kids import DEFAULT_AGES, DEFAULT_MAX_AGE, KidsPolicy, check_labels


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Assistant(_Strict):
    name: str
    vpn_name: str = (
        "the VPN"  # fills {vpn} in the TV's strings (its VPN warnings); the TV's built-in default is the same
    )


class Server(_Strict):
    host: str
    port: int = Field(ge=1, le=65535)
    db_path: Path
    log_path: Path
    lookup_concurrency: int = Field(ge=1)


class Answers(_Strict):
    picks: int = Field(ge=1, le=3)
    candidates: int = Field(default=20, ge=1)  # titles whose details are looked up per search
    hours_candidates: int = Field(default=40, ge=1)  # the same, for searches with a length filter

    @model_validator(mode="after")
    def _enough_candidates(self):
        if self.candidates < self.picks:
            raise ValueError(f"candidates {self.candidates} is below picks {self.picks}")
        if self.hours_candidates < self.candidates:
            raise ValueError(f"hours_candidates {self.hours_candidates} is below candidates {self.candidates}")
        return self


EXCLUDE_TMDB_FORMAT = re.compile(r"(movie|tv):[1-9][0-9]*")


class Ranking(_Strict):
    # genres a non-kids search drops unless a liked title or a requested genre has them: Animation, Kids,
    # Family, Reality, Talk, Soap, News, Documentary
    off_genres_unless_liked: list[int] = [16, 10762, 10751, 10764, 10767, 10766, 10763, 99]
    min_votes: int = Field(default=300, ge=0)  # vote_count floor for genre and like discovers, kids aside (0 = none)
    kids_min_votes: int = Field(default=50, ge=0)  # the same in kids mode: children's titles get fewer votes (0 = none)
    # "like" score: overlap_weight per liked title's list a candidate is in + bridged_weight if it came from the
    # genre-bridged discover + genre_weight x genre fit (0..1: how closely its genres match the liked titles',
    # relative to the best match of its kind); ties broken by vote_average pulled toward bayes_mean by bayes_min_votes
    overlap_weight: int = Field(default=2, ge=0)
    bridged_weight: int = Field(default=1, ge=0)
    genre_weight: float = Field(default=6.0, ge=0)
    bayes_min_votes: int = Field(default=200, ge=0)
    bayes_mean: float = 6.5
    # share of a like-question's lookup pool kept for titles from discover (on our services), since TMDB similar
    # rows mostly aren't on them
    discover_share: float = Field(default=0.75, ge=0, le=1)
    # final "like" ranking, once titles' details are known: keyword_weight x shared keywords (rare ones count more,
    # relative to the best candidate), franchise_weight for a title named like the liked one ("Star Trek: Picard")
    # that also shares keywords with it, plus overlap_weight per liked title recommending it and final_genre_weight x
    # genre fit as above (keywords also count for less when the genres disagree)
    keyword_weight: float = Field(default=6.0, ge=0)
    franchise_weight: float = Field(default=3.0, ge=0)
    final_genre_weight: float = Field(default=2.0, ge=0)
    # a liked title is the most-voted of the first liked_from_top search hits (1 = TMDB's first hit)
    liked_from_top: int = Field(default=3, ge=1)
    # titles never suggested, in any mode: "movie:<tmdb id>" or "tv:<tmdb id>" (movie and tv ids overlap)
    exclude_tmdb: list[str] = []

    @field_validator("exclude_tmdb")
    @classmethod
    def _kind_qualified_ids(cls, v: list[str]) -> list[str]:
        for entry in v:
            if not EXCLUDE_TMDB_FORMAT.fullmatch(entry):
                raise ValueError(f'exclude_tmdb entry {entry!r} is not "movie:<id>" or "tv:<id>"')
        return v

    def blocked(self) -> set[tuple[int, str]]:
        """exclude_tmdb as (tmdb id, kind) pairs."""
        return {(int(i), k) for k, i in (e.split(":") for e in self.exclude_tmdb)}


class Language(_Strict):
    code: str


class Cache(_Strict):
    warm_at: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    warm_genres: list[str] = ["comedy", "animation", "family", "kids", "sci-fi", "action", "drama", "documentary"]


class Budgets(_Strict):
    looking_ms: int
    posters_p50_ms: int
    followup_server_ms: int
    find_p95_cold_ms: int = 3000  # a cold "like X" search makes ~60 TMDB calls; measured ~3 s
    find_p95_warm_ms: int
    render_ms: int
    # asking from the TV (defaults, so a config from before [ask] still loads)
    listen_ms: int = 1000  # mic press -> listening on screen
    heard_ms: int = 3000  # end of speech -> "Looking for ..." on screen
    ask_posters_p50_ms: int = 12000  # end of speech -> posters on screen, p50
    ask_posters_p95_ms: int = 15000  # ... and p95 (scripts/asks_report.py judges the asks log against it)


# The most [ask] max_question_chars may be; ask.AskRequest caps `text` at it before the configured limit applies
QUESTION_CHARS_MAX = 1000


class Ask(_Strict):
    """Questions asked on the TV itself, answered by `claude -p` on the server."""

    enabled: bool = True
    claude_path: str = "claude"  # the Claude Code CLI
    model: str = "claude-haiku-5-5"
    # `claude --effort`: "low" keeps TV asks quick (little thinking); "default" passes no flag (the CLI's own default)
    effort: Literal["low", "medium", "high", "xhigh", "max", "default"] = "low"
    new_conversation_after_s: int = Field(
        default=600, ge=60
    )  # an ask this long after the last one starts a new conversation
    timeout_s: float = Field(default=45.0, gt=0)  # one Claude run; then the TV is told `timeout`
    # a longer question is refused (400); the TV reads the cap from hello.limits
    max_question_chars: int = Field(default=200, ge=1, le=QUESTION_CHARS_MAX)
    workdir: Path = Path("claude-home")  # Claude Code's working directory for TV asks
    mcp_url: str | None = None  # None: http://127.0.0.1:{server.port}/mcp
    # Caps on asks (429 "limit" past them): per_minute stops a runaway client on the LAN, per_day keeps the Claude
    # subscription for the evening (counted from local midnight)
    per_minute: int = Field(default=6, ge=1)
    per_day: int = Field(default=150, ge=1)


class Speech(_Strict):
    """Speech-to-text on the server for questions spoken into the TV remote (`POST /ask/audio`)."""

    engine: Literal["none", "faster-whisper"] = "none"  # faster-whisper needs `uv sync --extra speech`
    model: str = "base.en"
    device: str = "cpu"
    compute_type: str = "int8"
    cpu_threads: int = Field(default=4, ge=1)
    beam_size: int = Field(default=1, ge=1)
    language: str = "en"
    max_audio_s: float = Field(default=15, gt=0)  # longer clips are refused (413); the TV reads it from hello.limits
    timeout_s: float = Field(default=10, gt=0)  # one transcription; then the TV is told `failed`
    download_dir: Path = Path("models")  # where the model is downloaded and kept
    # spoken questions treated as nothing heard (Whisper's usual words on noise); typed ones are never dropped
    ignore_phrases: list[str] = ["thank you", "thanks for watching", "you"]


class Timeouts(_Strict):
    tmdb_s: float = Field(gt=0)
    tv_ack_s: float = Field(gt=0)
    wikidata_s: float = Field(default=2.0, gt=0)  # one Wikidata lookup for a title's ids (Google TV, Netflix)


class Ratings(_Strict):
    country: str
    fallback_country: str
    fallback_map: dict[str, str]


HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class Kids(_Strict):
    order: list[str]
    toddler_below_age: int
    toddler_allowed: list[str]
    no_age_max: str
    # label -> minimum age; every label in order, toddler_allowed, no_age_max and every [ratings] fallback_map value
    # must be here (the labels themselves are free: "G"/"PG"/"PG-13"/"R" works with a table of its own)
    ages: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=lambda: dict(DEFAULT_AGES))
    # a kids_age above this counts as this: the highest kids cutoff is the label with the largest age at most max_age
    max_age: int = Field(default=DEFAULT_MAX_AGE, ge=0)
    # Between these local times kids mode is on unless someone says the answer is for grown-ups (children may be
    # watching); outside them only when asked. None (either): only when asked.
    default_from: str | None = Field(default=None, pattern=HHMM)
    default_until: str | None = Field(default=None, pattern=HHMM)

    def default_on(self, now: datetime) -> bool:
        """Whether kids mode is on by default at local time `now`."""
        if not self.default_from or not self.default_until:
            return False
        t = now.strftime("%H:%M")
        start, end = self.default_from, self.default_until
        return start <= t < end if start <= end else (t >= start or t < end)

    @model_validator(mode="after")
    def _values_in_order(self):
        if self.no_age_max not in self.order:
            raise ValueError(f"no_age_max {self.no_age_max!r} is not in order {self.order}")
        for r in self.toddler_allowed:
            if r not in self.order:
                raise ValueError(f"toddler_allowed entry {r!r} is not in order {self.order}")
        check_labels(self.order, self.toddler_allowed, self.no_age_max, self.ages)
        return self


ID_PLACEHOLDERS = frozenset({"netflix_id"})  # looked up per title (title_ids.py); unknown id = step skipped
LAUNCH_PLACEHOLDERS = frozenset({"title", "title_url", "tmdb_id", "year"}) | ID_PLACEHOLDERS


class LaunchCfg(_Strict):
    """How to open a title in the app. `data` and `extras` values may use {title}, {title_url} (URL-encoded),
    {tmdb_id}, {year} (empty when unknown) and {netflix_id} (the step is left out when the id is unknown).
    `component` ("package/.Activity") makes the intent explicit to one activity."""

    action: str
    data: str | None = None
    extras: dict[str, str] = {}
    component: str | None = None

    def placeholders(self) -> set[str]:
        texts = ([self.data] if self.data else []) + list(self.extras.values())
        return {name for t in texts for name in re.findall(r"\{([^{}]*)\}", t)}

    @model_validator(mode="after")
    def _known_placeholders(self):
        for name in sorted(self.placeholders()):
            if name not in LAUNCH_PLACEHOLDERS:
                raise ValueError(f"unknown placeholder {{{name}}} (use {sorted(LAUNCH_PLACEHOLDERS)})")
        return self


class Open(_Strict):
    """Google TV's own title page for a pick (services with google_tv = true). `google_tv_url` takes
    {google_id_url}: the title's Google Knowledge Graph id ("/g/11f7dr1t_l"), URL-encoded."""

    google_tv_package: str = "com.google.android.apps.tv.launcherx"
    google_tv_url: str = "https://tv.google.com/asset/{google_id_url}"
    # show_on_tv/open_title wait this long for title ids still being looked up (find_titles starts the lookups), then
    # send without the steps that need them (the lookup still finishes in the background and is cached)
    title_id_wait_s: float = Field(default=0.25, ge=0)
    # Wikidata asks API clients to name a contact (URL or email) in the User-Agent; empty = just "lumi-tv/<version>"
    user_agent_contact: str = Field(default="", max_length=200)
    # Wikidata ids rarely change (kept this long); a title without ids is checked again sooner, since new titles
    # often get their Netflix id days after release
    wikidata_ttl_days: float = Field(default=30, gt=0)
    wikidata_miss_ttl_days: float = Field(default=7, gt=0)

    @field_validator("google_tv_url")
    @classmethod
    def _one_placeholder(cls, v: str) -> str:
        names = re.findall(r"\{([^{}]*)\}", v)
        if names != ["google_id_url"]:
            raise ValueError(f"google_tv_url must contain {{google_id_url}} once and no other placeholder: {v!r}")
        return v


class ServiceCfg(_Strict):
    id: str
    label: str
    region: str
    tmdb_provider_ids: list[int]
    package: str
    vpn: Literal["on", "off", "any"]
    launch: LaunchCfg | None = None  # none: only open the app
    search_hint: bool = False  # the launch opens the app's search screen but can't fill it in
    google_tv: bool = False  # try Google TV's title page for the pick first (see [open])
    # the app drops a link on a cold start (profile picker first): the TV sends the launch again this many
    # seconds later, while the app stays in front
    relaunch_after_s: list[float] = []

    @field_validator("relaunch_after_s")
    @classmethod
    def _positive_increasing(cls, v: list[float]) -> list[float]:
        if any(x <= 0 for x in v) or v != sorted(set(v)):
            raise ValueError(f"relaunch_after_s must be increasing positive seconds: {v}")
        return v


class Config(_Strict):
    assistant: Assistant
    server: Server
    answers: Answers
    language: Language
    ranking: Ranking = Ranking()
    cache: Cache
    budgets: Budgets
    ask: Ask = Ask()
    speech: Speech = Speech()
    timeouts: Timeouts
    ratings: Ratings
    kids: Kids
    open: Open = Open()
    services: list[ServiceCfg]
    tmdb_token: str = Field(min_length=1, repr=False)
    server_token: str = Field(min_length=1, repr=False)
    claude_oauth_token: str | None = Field(None, repr=False)  # `claude setup-token`, for a server without a login

    @model_validator(mode="after")
    def _fallback_labels_have_ages(self):
        for label, mapped in self.ratings.fallback_map.items():
            if mapped not in self.kids.ages:
                raise ValueError(
                    f"ratings.fallback_map {label} = {mapped!r} has no age in [kids] ages {sorted(self.kids.ages)}"
                )
        return self

    def kids_policy(self) -> KidsPolicy:
        return KidsPolicy(**self.kids.model_dump())


def load(path: Path | str = "lumitv.toml", env_file: Path | str | None = None) -> Config:
    """Secrets from .env (TMDB_TOKEN, SERVER_TOKEN, optional CLAUDE_CODE_OAUTH_TOKEN; process env overrides), the rest
    from TOML.
    Raises ValueError naming the offending key on a missing or invalid value."""
    path = Path(path)
    with path.open("rb") as f:
        data = tomllib.load(f)
    env = {**dotenv_values(env_file or path.parent / ".env"), **os.environ}
    data["tmdb_token"] = env.get("TMDB_TOKEN")
    data["server_token"] = env.get("SERVER_TOKEN")
    data["claude_oauth_token"] = env.get("CLAUDE_CODE_OAUTH_TOKEN") or None
    names = {"tmdb_token": "TMDB_TOKEN", "server_token": "SERVER_TOKEN"}
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        problems = []
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"])
            loc = names.get(loc, loc)
            problems.append(f"{loc}: {err['msg']}")
        raise ValueError("invalid configuration: " + "; ".join(problems)) from None
