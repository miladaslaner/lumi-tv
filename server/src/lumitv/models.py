from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Kind = Literal["movie", "tv"]
ServiceId = str  # services are configuration; validated against the config


class TitleCard(BaseModel):
    token: str
    tmdb_id: int
    kind: Kind
    title: str
    year: int | None
    overview: str
    poster_url: str | None
    services: list[ServiceId]  # the chosen service first
    service: ServiceId | None = None  # the service this title opens in (the requested one, else config order)
    asked_service: ServiceId | None = None  # the service find_titles was asked for, if any: it opens there first
    certification: str | None
    minutes: int | None  # movie runtime, or season 1 for series
    total_minutes: int | None  # series only
    seasons: int | None
    seen: bool
    binge_seasons: int | None = None  # with an hours filter, series only: None = whole run, 1 or 2 = first N seasons
    binge_minutes: int | None = None  # the length of the span that fit the hours
    matches: list[str] = []  # "like X": the keywords it shares with X, strongest first ("space opera")

    @property
    def primary_service(self) -> ServiceId | None:
        return self.service or (self.services[0] if self.services else None)


class Launch(BaseModel):
    """One way the TV can open a title. Built server-side so deep links can change without rebuilding the app."""

    package: str
    action: str
    data: str | None = None
    extras: dict[str, str] = {}
    component: str | None = None  # "package/.Activity": explicit to that activity
    vpn: Literal["on", "off", "any"]  # the service's VPN need (also on the Google TV step: the title plays there)
    hint: bool = False  # this step lands on the app's empty search screen: the TV shows the pick's search_hint
    relaunch_after_s: list[float] = []  # cold app start: send this step again after these seconds (TV decides)
    fallback: "Launch | None" = None  # only in the legacy `launch` chain for older TV apps; see launch_chain


ADULT_AGE = 18  # a viewer with an age below this is a child: the youngest one watching sets the kids limit
MAX_VIEWERS = 12
MAX_VIEWER_NAME = 40
# names travel comma-joined in `?viewers=` and newline-joined in the store: neither character can be part of one
VIEWER_NAME_PATTERN = r"^[^\r\n,]+$"


class Viewer(BaseModel):
    """Someone who watches this TV (set up on the TV, kept in the household store, never in the repo)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=MAX_VIEWER_NAME, pattern=VIEWER_NAME_PATTERN)
    age: int | None = Field(default=None, ge=0, le=120)  # set for children; None = grown-up

    @property
    def is_child(self) -> bool:
        return self.age is not None and self.age < ADULT_AGE

    def label(self) -> str:
        return f"{self.name} ({self.age})" if self.age is not None else self.name
