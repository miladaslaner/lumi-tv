"""Lists what find_titles returns for "something like X" (and mood/genre questions) against real TMDB: what X was
matched to, and the titles with their services, TMDB genres and shared keywords. No verdicts: a before/after diff of
the output shows what a ranking change did (scripts/quality_check.py is the scored snapshot).

Run from lumi-tv/server:
    uv run python scripts/like_eval.py [--config lumitv.toml] [--db .tmp/like_eval.db] [--case "Star Trek"]
Needs TMDB_TOKEN in .env next to the config (never printed). The cache DB is kept between runs, so repeat runs are
fast and spare TMDB; delete it to measure fresh data.
"""

import argparse
import asyncio
from pathlib import Path

from lumitv import config, services as services_mod
from lumitv.catalog import LookupStats, PickRegistry, find_titles, load_genre_aliases, load_genre_keywords
from lumitv.tmdb import Cache, Tmdb
from lumitv.tools import INSTRUCTIONS_PATH

ROOT = Path(__file__).resolve().parents[1]
# (label, find_titles inputs): what Claude passes for the question in the label
CASES: list[tuple[str, dict]] = [
    ("like Star Trek", dict(like=["Star Trek"])),
    ("like The Expanse", dict(like=["The Expanse"])),
    ("like Suits", dict(like=["Suits"])),
    ("like Spider-Man", dict(like=["Spider-Man"])),
    ("like Friends", dict(like=["Friends"])),
    ("like Breaking Bad", dict(like=["Breaking Bad"])),
    ("like The Office", dict(like=["The Office"])),
    ("like Interstellar", dict(like=["Interstellar"])),
    ("like Game of Thrones", dict(like=["Game of Thrones"])),
    ("like Ted Lasso", dict(like=["Ted Lasso"])),
    ("films like The Matrix", dict(like=["The Matrix"], kind="movie")),
    ("series like Sherlock", dict(like=["Sherlock"], kind="tv")),
    # mood / genre questions (no like): the other half of "what should we watch"
    ("something funny", dict(genres=["comedy"])),
    ("a scary film", dict(genres=["horror"], kind="movie")),
    ("a sci-fi series", dict(genres=["sci-fi"], kind="tv")),
    ("a fantasy series", dict(genres=["fantasy"], kind="tv")),
    ("a calm film for a 5 year old", dict(genres=["animation", "family"], kind="movie", kids_age=5)),
    ("a thriller series to binge", dict(genres=["thriller"], kind="tv", min_hours=8, max_hours=20)),
]


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, default=ROOT / "lumitv.example.toml")
    p.add_argument("--db", type=Path, default=ROOT / ".tmp" / "like_eval.db")
    p.add_argument("--case", action="append", help="only cases whose label contains this (repeatable)")
    args = p.parse_args()
    cfg = config.load(args.config, env_file=ROOT / ".env")
    services = services_mod.load(cfg)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    cache = Cache(args.db)
    tmdb = Tmdb(cfg.tmdb_token, cache, cfg.ratings, cfg.timeouts.tmdb_s, ages=cfg.kids.ages,
                concurrency=cfg.server.lookup_concurrency)  # fmt: skip
    aliases = load_genre_aliases(INSTRUCTIONS_PATH.parent, cfg.language.code)
    genre_kw = load_genre_keywords(INSTRUCTIONS_PATH.parent, cfg.language.code)
    for label, kw in CASES:
        if args.case and not any(c.lower() in label.lower() for c in args.case):
            continue
        hits = await tmdb.search(kw["like"][0], None) if kw.get("like") else []
        top = hits[0] if hits else {}
        stats = LookupStats()
        cards = await find_titles(
            tmdb, PickRegistry(), set(), services, cfg.kids_policy(), limit=6,
            concurrency=cfg.server.lookup_concurrency, candidates=cfg.answers.candidates,
            hours_candidates=cfg.answers.hours_candidates, genre_aliases=aliases, genre_keywords=genre_kw,
            ranking=cfg.ranking, stats=stats, **kw,
        )  # fmt: skip
        print(
            f"\n=== {label}   (search top: {top.get('title') or top.get('name')} "
            f"{top.get('media_type')}:{top.get('id')})"
            f"  no_titles_on={stats.no_titles_on}"
        )
        for c in cards:
            raw = await tmdb._get(f"/{c.kind}/{c.tmdb_id}")
            g = ", ".join(x["name"] for x in raw.get("genres", []))
            print(f"  {c.title} ({c.year}, {c.kind}) · {'/'.join(c.services)} · {g} · matches: {', '.join(c.matches)}")
    cache.close()


if __name__ == "__main__":
    asyncio.run(main())
