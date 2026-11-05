import re
from pathlib import Path

import pytest

from lumitv import config, services

EXAMPLE = Path(__file__).parent.parent / "lumitv.example.toml"


def _cfg(tmp_path, toml=None):
    env = tmp_path / ".env"
    env.write_text("TMDB_TOKEN=fake-tmdb\nSERVER_TOKEN=fake-server\n")
    path = EXAMPLE
    if toml is not None:
        path = tmp_path / "lumitv.toml"
        path.write_text(toml)
    return config.load(path, env_file=env)


def test_example_services(tmp_path):
    S = services.load(_cfg(tmp_path))
    assert len(S) == 4
    assert S["videoland"].vpn == "on"
    assert S["netflix"].region == "AE"
    assert S["netflix"].package == "com.netflix.ninja"
    assert S["youtube"].tmdb_provider_ids == frozenset()
    assert services.service_for_provider(S, 8, "AE") == "netflix"
    assert services.service_for_provider(S, 72, "NL") == "videoland"
    assert services.service_for_provider(S, 119, "AE") == "prime"
    assert services.service_for_provider(S, 999999, "AE") is None
    assert services.service_for_provider(S, 72, "AE") is None


def test_duplicate_service_id_rejected(tmp_path):
    dup = (
        EXAMPLE.read_text(encoding="utf-8") + '\n[[services]]\nid = "netflix"\nlabel = "N2"\nregion = "NL"\n'
        'tmdb_provider_ids = [8]\npackage = "x"\nvpn = "on"\n'
    )
    with pytest.raises(ValueError, match="netflix"):
        services.load(_cfg(tmp_path, dup))


def test_kids_values_must_be_in_order(tmp_path):
    bad = re.sub(r"(?m)^no_age_max\s*=.*$", 'no_age_max = "99"', EXAMPLE.read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="no_age_max"):
        _cfg(tmp_path, bad)


def _pick(*svcs, asked=None):
    from lumitv.catalog import Pick

    return Pick(
        token="t",
        tmdb_id=1,
        kind="tv",
        title="Suits",
        year=2011,
        overview="",
        poster_url=None,
        services=list(svcs),
        service=svcs[0],
        asked_service=asked,
        certification="12",
        minutes=40,
        total_minutes=None,
        seasons=None,
        seen=False,
        kids_mode=False,
        kids_age=None,
        seq=1,
    )


def test_provider_order_follows_ask_then_history_then_exact_title_then_vpn_then_config(tmp_path):
    S = services.load(_cfg(tmp_path))
    p = _pick("netflix", "prime", "videoland")
    order = lambda **kw: services.provider_order(p, S, **{"used": set(), "ids": {}, "vpn_on": False, **kw})  # noqa: E731
    # nothing else to go on: the config order
    assert order() == ["netflix", "prime", "videoland"]
    # VPN on: Videoland needs it on, Netflix wants it off
    assert order(vpn_on=True) == ["prime", "videoland", "netflix"]
    # Netflix opens the exact title with its id; Prime only with Google TV's page id
    assert order(ids={"google_id": "/g/1"}, vpn_on=True)[0] == "prime"
    assert order(ids={"netflix_id": "70195800"}, vpn_on=True)[0] == "netflix"
    # watched there before beats both
    assert order(used={"videoland"}, ids={"netflix_id": "7"})[0] == "videoland"
    # and the service the user asked for beats everything
    asked = _pick("netflix", "prime", "videoland", asked="prime")
    assert services.provider_order(asked, S, used={"videoland"}, ids={}, vpn_on=False)[0] == "prime"


def test_a_service_that_lately_failed_goes_last_unless_asked_for(tmp_path):
    S = services.load(_cfg(tmp_path))
    p = _pick("netflix", "prime", "videoland")
    assert services.provider_order(p, S, used={"netflix"}, ids={}, vpn_on=False, bad={"netflix"})[-1] == "netflix"
    asked = _pick("netflix", "prime", asked="netflix")
    assert services.provider_order(asked, S, used=set(), ids={}, vpn_on=False, bad={"netflix"})[0] == "netflix"
