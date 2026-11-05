"""Speed and quality bench for find_titles against real TMDB.

Run from lumi-tv/server:
    uv run python scripts/bench.py [--config lumitv.example.toml]
Needs TMDB_TOKEN in .env next to the config (never printed). Three runs, each on a fresh temp cache DB:
cold (empty cache), warm-repeat (same DB straight after), after-warm (fresh DB, warm_detailed, then the calls).
"""

import argparse
import asyncio
import tempfile
import time
from pathlib import Path

from lumitv import config, services as services_mod, warm as warm_mod
from lumitv.catalog import PickRegistry, find_titles, load_genre_aliases, load_genre_keywords
from lumitv.tmdb import Cache, Tmdb
from lumitv.tools import INSTRUCTIONS_PATH, length_label

ROOT = Path(__file__).resolve().parents[1]
TRIO = ["The Matrix", "The Lord of the Rings", "Star Trek"]
CALLS: list[tuple[str, dict]] = [
    ("binge tv", dict(like=TRIO, kind="tv", min_hours=10, max_hours=15)),
    ("lighter", dict(like=TRIO, min_hours=10, max_hours=15, genres=["comedy"])),
    ("spider-man", dict(like=["Spider-Man"])),
    ("suits netflix tv", dict(like=["Suits"], service="netflix", kind="tv")),
]


async def run_calls(cfg, services, tmdb, counter, aliases, keywords, label: str) -> list[int]:
    print(f"\n=== {label} ===")
    times = []
    for name, kw in CALLS:
        counter["hit"] = counter["miss"] = 0
        started = time.perf_counter()
        # the server's own call (tools.py), so the keyword rule for genre searches is part of the timing
        cards = await find_titles(
            tmdb, PickRegistry(), set(), services, cfg.kids_policy(),
            limit=6, concurrency=cfg.server.lookup_concurrency, candidates=cfg.answers.candidates,
            hours_candidates=cfg.answers.hours_candidates, genre_aliases=aliases, genre_keywords=keywords,
            ranking=cfg.ranking, **kw,
        )  # fmt: skip
        ms = round((time.perf_counter() - started) * 1000)
        times.append(ms)
        print(f"{name:18} {ms:6d} ms  misses={counter['miss']:3d} hits={counter['hit']:3d}  titles={len(cards)}")
        for c in cards:
            on = "/".join(services[s].label for s in c.services)
            print(f"    - {c.title} ({c.year}, {c.kind}) [{on}] {length_label(c)}")
    return times


async def main(config_path: Path) -> None:
    cfg = config.load(config_path, env_file=ROOT / ".env")
    services = services_mod.load(cfg)
    aliases = load_genre_aliases(INSTRUCTIONS_PATH.parent, cfg.language.code)
    keywords = load_genre_keywords(INSTRUCTIONS_PATH.parent, cfg.language.code)
    summary: dict[str, list[int]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        counter = {"hit": 0, "miss": 0}

        def make(db: Path) -> Tmdb:
            def note(hit: bool) -> None:
                counter["hit" if hit else "miss"] += 1

            return Tmdb(cfg.tmdb_token, Cache(db), cfg.ratings, cfg.timeouts.tmdb_s, on_cache=note)

        tmdb = make(Path(tmp) / "cold.db")
        summary["cold"] = await run_calls(cfg, services, tmdb, counter, aliases, keywords, "cold (fresh DB)")
        summary["warm-repeat"] = await run_calls(
            cfg, services, tmdb, counter, aliases, keywords, "warm-repeat (same DB)"
        )
        await tmdb.aclose()

        tmdb = make(Path(tmp) / "warmed.db")
        started = time.perf_counter()
        r = await warm_mod.warm_detailed(
            tmdb, services, max(1, cfg.server.lookup_concurrency // 2), cfg.cache.warm_genres, aliases,
            min_votes=cfg.ranking.min_votes or None, genre_keywords=keywords,
        )  # fmt: skip
        print(
            f"\nwarm_detailed: {round(time.perf_counter() - started)} s, titles={r.warmed}/{r.attempted}, "
            f"discover_failed={r.discover_failed}, exact_failed={r.exact_failed}"
        )
        summary["after-warm"] = await run_calls(
            cfg, services, tmdb, counter, aliases, keywords, "after-warm (fresh DB + warm_detailed)"
        )
        await tmdb.aclose()
    print("\n=== summary (ms) ===")
    print(f"{'':14}" + "".join(f"{n:>18}" for n, _ in CALLS))
    for k, v in summary.items():
        print(f"{k:14}" + "".join(f"{x:18d}" for x in v))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=ROOT / "lumitv.example.toml")
    asyncio.run(main(ap.parse_args().config))
