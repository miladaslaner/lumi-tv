import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

from .config import ID_PLACEHOLDERS, Config, LaunchCfg, Open
from .models import Launch

if TYPE_CHECKING:
    from .catalog import Pick


@dataclass(frozen=True)
class Service:
    id: str
    label: str
    tmdb_provider_ids: frozenset[int]
    region: str  # TMDB watch_region; "" when the service has no TMDB source
    package: str  # Android package on the TV
    vpn: Literal["on", "off", "any"]  # required NordVPN state for the title to open
    launch: LaunchCfg | None = None  # None: only open the app
    search_hint: bool = False  # the launch lands on an empty search screen: the TV shows the title to type
    google_tv: bool = False  # Google TV's title page for the pick comes first, when its Google id is known
    relaunch_after_s: tuple[float, ...] = ()  # resend the launch after a cold start (see ServiceCfg)

    @property
    def wanted_ids(self) -> frozenset[str]:
        """The per-title ids this service's launch uses (title_ids.py)."""
        ids = {"google_id"} if self.google_tv else set()
        if self.launch:
            ids |= self.launch.placeholders() & ID_PLACEHOLDERS
        return frozenset(ids)


def load(cfg: Config) -> dict[str, Service]:
    """Build the service table from `[[services]]`. Rejects duplicate ids."""
    out: dict[str, Service] = {}
    for s in cfg.services:
        if s.id in out:
            raise ValueError(f"invalid configuration: services: duplicate id {s.id!r}")
        out[s.id] = Service(
            s.id, s.label, frozenset(s.tmdb_provider_ids), s.region, s.package, s.vpn, s.launch, s.search_hint,
            s.google_tv, tuple(s.relaunch_after_s),
        )  # fmt: skip
    return out


def service_for_provider(services: dict[str, Service], provider_id: int, region: str) -> str | None:
    """Service id for a TMDB provider in a region (ids repeat across regions), else None."""
    for s in services.values():
        if s.region == region and provider_id in s.tmdb_provider_ids:
            return s.id
    return None


def _fill(template: str, values: dict[str, str]) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: values[m.group(1)], template)


VIEW = "android.intent.action.VIEW"
DEFAULT_OPEN = Open()


def google_tv_url(template: str, google_id: str) -> str:
    """Google TV's title page for a Google Knowledge Graph id ("/g/11f7dr1t_l" -> "%2Fg%2F11f7dr1t_l")."""
    return template.replace("{google_id_url}", quote(google_id, safe=""))


def opens_exact_title(svc: Service, ids: dict[str, str] | None) -> bool:
    """Whether `svc` can open the title's own page (not a search or the app) with these ids."""
    ids = ids or {}
    if svc.google_tv and ids.get("google_id"):
        return True
    needs = svc.launch.placeholders() & ID_PLACEHOLDERS if svc.launch else set()
    return bool(needs) and needs <= ids.keys()


def provider_order(
    pick: "Pick",
    services: dict[str, Service],
    used: set[str],
    ids: dict[str, str] | None,
    vpn_on: bool,
    bad: set[str] = frozenset(),  # type: ignore[assignment]
) -> list[str]:
    """The pick's services, best to open first: the one the user asked for, then any not in `bad` (lately failed to
    open or play this title), then one the family watched or opened this title on, then one that opens the exact
    title, then one that works with the TV's VPN as it is now, then config order."""
    config_order = list(services)

    def vpn_fits(sid: str) -> bool:
        need = services[sid].vpn
        return need == "any" or (need == "on") == vpn_on

    return sorted(
        (s for s in pick.services if s in services),
        key=lambda s: (
            s != pick.asked_service,
            s in bad,
            s not in used,
            not opens_exact_title(services[s], ids),
            not vpn_fits(s),
            config_order.index(s),
        ),
    )


def launch_steps(
    pick: "Pick",
    services: dict[str, Service],
    ids: dict[str, str] | None = None,
    open_cfg: Open = DEFAULT_OPEN,
    service: str | None = None,
) -> list[Launch]:
    """The ways to open the pick on `service` (default: its primary service), in the order the TV tries them:
    Google TV's title page (services with google_tv, when `ids` has its google_id), the service's own `launch`
    template filled with the title (left out when it needs an id `ids` lacks), and last the plain app launch. Every
    step carries the service's VPN need."""
    ids = ids or {}
    google_id = ids.get("google_id")
    sid = service or pick.primary_service
    if sid is None:
        raise ValueError(f"pick {pick.token} has no service to launch")
    svc = services[sid]
    steps: list[Launch] = []
    if svc.google_tv and google_id:
        steps.append(
            Launch(
                package=open_cfg.google_tv_package,
                action=VIEW,
                data=google_tv_url(open_cfg.google_tv_url, google_id),
                vpn=svc.vpn,
            )
        )
    t = svc.launch
    if t is not None and t.placeholders() & ID_PLACEHOLDERS <= ids.keys():
        values = {
            "title": pick.title,
            "title_url": quote(pick.title, safe=""),
            "tmdb_id": str(pick.tmdb_id),
            "year": "" if pick.year is None else str(pick.year),
            **{k: quote(v, safe="") for k, v in ids.items() if k in ID_PLACEHOLDERS},
        }
        steps.append(
            Launch(
                package=svc.package,
                action=t.action,
                data=_fill(t.data, values) if t.data else None,
                extras={k: _fill(v, values) for k, v in t.extras.items()},
                component=t.component,
                vpn=svc.vpn,
                hint=svc.search_hint,
                relaunch_after_s=list(svc.relaunch_after_s),
            )
        )
    steps.append(Launch(package=svc.package, action="android.intent.action.MAIN", vpn=svc.vpn))
    return steps


def launch_chain(steps: list[Launch]) -> Launch:
    """The steps as one Launch nested through `fallback`: the protocol's legacy `launch` field, which TV apps
    from before the `launches` list read."""
    out: Launch | None = None
    for step in reversed(steps):
        out = step.model_copy(update={"fallback": out})
    assert out is not None
    return out


def launch_for(
    pick: "Pick", services: dict[str, Service], ids: dict[str, str] | None = None, open_cfg: Open = DEFAULT_OPEN
) -> Launch:
    """launch_steps as a fallback chain."""
    return launch_chain(launch_steps(pick, services, ids, open_cfg))
