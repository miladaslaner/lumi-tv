from pathlib import Path

import pytest

from lumitv import config, services
from lumitv.catalog import Pick
from lumitv.config import LaunchCfg
from lumitv.services import launch_for

EXAMPLE = Path(__file__).parent.parent / "lumitv.example.toml"


def _services(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TMDB_TOKEN=x\nSERVER_TOKEN=y\n")
    return services.load(config.load(EXAMPLE, env_file=env))


def _pick(sid="netflix", all_services=None, title="Tom & Jerry: The Movie", year=1992, **kw):
    return Pick(
        token="t",
        tmdb_id=77,
        kind="movie",
        title=title,
        year=year,
        overview="",
        poster_url=None,
        services=all_services or [sid],
        service=sid,
        certification="AL",
        minutes=90,
        total_minutes=None,
        seasons=None,
        seen=False,
        kids_mode=False,
        kids_age=None,
        seq=1,
        **kw,
    )


def test_netflix_without_its_title_id_opens_the_app(tmp_path):
    # verified on a real TV: no public Netflix link opens its search; the title link needs the Netflix id
    S = _services(tmp_path)
    ln = launch_for(_pick(), S)
    assert ln.package == "com.netflix.ninja" and ln.action == "android.intent.action.MAIN"
    assert ln.data is None and ln.fallback is None and ln.vpn == "off"
    assert not S["netflix"].search_hint


def test_netflix_title_link_then_app(tmp_path):
    S = _services(tmp_path)
    steps = launch_steps(_pick(), S, {"netflix_id": "70195800", "google_id": "/m/0gg70vv"})
    assert [s.action for s in steps] == ["android.intent.action.VIEW", "android.intent.action.MAIN"]  # google_tv off
    link = steps[0]
    assert link.data == "https://www.netflix.com/title/70195800" and link.component == "com.netflix.ninja/.MainActivity"
    assert link.extras == {"source": "30"} and link.relaunch_after_s == [10] and not link.hint
    assert steps[1].relaunch_after_s == []  # only the title link is re-sent
    assert S["netflix"].wanted_ids == {"netflix_id"} and S["prime"].wanted_ids == {"google_id"}


def test_template_url_encoded_with_app_fallback(tmp_path):
    S = _services(tmp_path)
    t = LaunchCfg(action="android.intent.action.VIEW", data="https://example.test/search?q={title_url}")
    S["netflix"] = S["netflix"].__class__(**{**S["netflix"].__dict__, "launch": t})
    ln = launch_for(_pick(), S)
    assert ln.data == "https://example.test/search?q=Tom%20%26%20Jerry%3A%20The%20Movie"
    assert ln.fallback.action == "android.intent.action.MAIN" and ln.fallback.data is None
    assert ln.fallback.package == "com.netflix.ninja" and ln.fallback.fallback is None


def test_videoland_search_page_and_vpn_on(tmp_path):
    S = _services(tmp_path)
    ln = launch_for(_pick("videoland"), S)
    assert ln.action == "android.intent.action.VIEW" and ln.data == "https://v2.videoland.com/zoeken"
    assert ln.vpn == "on" and ln.package == "nl.rtl.videoland.v2"
    assert ln.fallback.action == "android.intent.action.MAIN"
    assert S["videoland"].search_hint and S["prime"].search_hint and not S["youtube"].search_hint


def test_extras_are_filled_raw_not_url_encoded(tmp_path):
    S = _services(tmp_path)
    t = LaunchCfg(action="android.intent.action.SEARCH", extras={"query": "{title}"})
    S["videoland"] = S["videoland"].__class__(**{**S["videoland"].__dict__, "launch": t})
    assert launch_for(_pick("videoland"), S).extras == {"query": "Tom & Jerry: The Movie"}


def test_prime_and_youtube_urls(tmp_path):
    S = _services(tmp_path)
    assert launch_for(_pick("prime", title="The Tick"), S).data == "https://app.primevideo.com/search?phrase=The%20Tick"
    yt = launch_for(_pick("youtube", title="Bluey"), S)
    assert yt.data == "https://www.youtube.com/results?search_query=Bluey" and yt.vpn == "any"


def test_no_launch_table_means_app_launch_only(tmp_path):
    S = _services(tmp_path)
    S["prime"] = S["prime"].__class__(**{**S["prime"].__dict__, "launch": None})
    ln = launch_for(_pick("prime"), S)
    assert ln.action == "android.intent.action.MAIN" and ln.data is None and ln.fallback is None


def test_all_placeholders(tmp_path):
    S = _services(tmp_path)
    t = LaunchCfg(action="X", data="u://{tmdb_id}/{year}", extras={"q": "{title} ({year})"})
    S["netflix"] = S["netflix"].__class__(**{**S["netflix"].__dict__, "launch": t})
    ln = launch_for(_pick(title="A{b}"), S)  # braces in a title are not placeholders
    assert ln.data == "u://77/1992" and ln.extras == {"q": "A{b} (1992)"}
    assert launch_for(_pick(year=None), S).data == "u://77/"


def test_launch_uses_the_pick_service_not_the_first_listed(tmp_path):
    S = _services(tmp_path)
    ln = launch_for(_pick("prime", all_services=["prime", "netflix"]), S)
    assert ln.package == "com.amazon.amazonvideo.livingroom"


def test_unknown_placeholder_rejected():
    with pytest.raises(ValueError, match="nope"):
        LaunchCfg(action="X", data="u://{nope}")


# ---- Google TV step and the ordered launch list ----

from lumitv.config import Open  # noqa: E402
from lumitv.services import launch_chain, launch_steps  # noqa: E402

UPLOAD = "/g/11f7dr1t_l"


def test_example_enables_google_tv_for_prime_only(tmp_path):
    S = _services(tmp_path)
    assert {sid for sid, s in S.items() if s.google_tv} == {"prime"}


def test_google_tv_page_first_then_service_search_then_app(tmp_path):
    S = _services(tmp_path)
    steps = launch_steps(_pick("prime", title="Upload"), S, {"google_id": UPLOAD})
    assert [(s.package, s.action) for s in steps] == [
        ("com.google.android.apps.tv.launcherx", "android.intent.action.VIEW"),
        ("com.amazon.amazonvideo.livingroom", "android.intent.action.VIEW"),
        ("com.amazon.amazonvideo.livingroom", "android.intent.action.MAIN"),
    ]
    assert steps[0].data == "https://tv.google.com/asset/%2Fg%2F11f7dr1t_l"
    assert [s.hint for s in steps] == [False, True, False]  # the search hint belongs to the search step only
    assert {s.vpn for s in steps} == {"any"}  # the VPN need stays the service's


def test_no_google_step_without_an_id_or_with_google_tv_off(tmp_path):
    S = _services(tmp_path)
    assert [s.action for s in launch_steps(_pick("prime"), S, None)] == [
        "android.intent.action.VIEW",
        "android.intent.action.MAIN",
    ]
    assert [s.package for s in launch_steps(_pick("netflix"), S, {"google_id": UPLOAD})] == [
        "com.netflix.ninja"
    ]  # google_tv = false


def test_google_tv_package_and_url_come_from_config(tmp_path):
    S = _services(tmp_path)
    cfg = Open(google_tv_package="x.launcher", google_tv_url="https://g.test/a?id={google_id_url}")
    first = launch_steps(_pick("prime"), S, {"google_id": "/m/0gg70vv"}, cfg)[0]
    assert first.package == "x.launcher" and first.data == "https://g.test/a?id=%2Fm%2F0gg70vv"


def test_google_tv_url_needs_its_placeholder():
    with pytest.raises(ValueError, match="google_id_url"):
        Open(google_tv_url="https://tv.google.com/asset/")
    with pytest.raises(ValueError, match="google_id_url"):
        Open(google_tv_url="https://tv.google.com/{title}/{google_id_url}")


def test_launch_chain_nests_the_steps_for_older_apps(tmp_path):
    S = _services(tmp_path)
    steps = launch_steps(_pick("prime"), S, {"google_id": UPLOAD})
    ln = launch_chain(steps)
    assert ln.package == "com.google.android.apps.tv.launcherx"
    assert ln.fallback.data.startswith("https://app.primevideo.com/") and ln.fallback.fallback.action.endswith("MAIN")
    assert ln.fallback.fallback.fallback is None
    assert launch_for(_pick("prime"), S, {"google_id": UPLOAD}) == ln


def test_relaunch_after_s_must_increase():
    from lumitv.config import ServiceCfg

    base = dict(id="x", label="X", region="", tmdb_provider_ids=[], package="p", vpn="any")
    assert ServiceCfg(**base, relaunch_after_s=[8, 16]).relaunch_after_s == [8, 16]
    for bad in ([16, 8], [0], [8, 8]):
        with pytest.raises(ValueError, match="relaunch_after_s"):
            ServiceCfg(**base, relaunch_after_s=bad)
