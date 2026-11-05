"""Regression snapshot of pick quality against real TMDB. Each case names titles a person would accept (at least
`want_count` of them must be among the six results), titles they would reject (`never`), or a kids limit (safety).
The run fails (exit 1) when fewer than PASS_MARK of the quality cases pass or any safety case fails.

This is a snapshot, not a quality measure: it catches a ranking that stops finding what it found, and a kids
filter that lets something through. Catalogs change, so expectations are sets, not exact lists. Cases marked
`known_weak` return titles from their `never` set today (the ranking matches them on one generic keyword, like
"office" or "american abroad"); those are printed and counted as weak picks instead of failing the run, so a fix
shows up as a falling count and a regression as a rising one.

Run from lumi-tv/server:
    uv run python scripts/quality_check.py [--config lumitv.example.toml] [--db .tmp/like_eval.db | --fresh]
Needs TMDB_TOKEN in .env (never printed). The default cache file is like_eval.py's, kept between runs; --fresh uses
a throwaway cache so every lookup goes to TMDB (~40 s).
"""

import argparse
import asyncio
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from lumitv import config, services as services_mod
from lumitv.catalog import PickRegistry, find_titles, load_genre_aliases, load_genre_keywords
from lumitv.kids import allowed
from lumitv.tmdb import Cache, Tmdb
from lumitv.tools import INSTRUCTIONS_PATH

ROOT = Path(__file__).resolve().parents[1]
PASS_MARK = 0.8  # share of quality cases that must pass; every safety case must pass
# never in a kids answer: NL rates both AL in cinemas, the US R / PG-13 (see [ratings] in the toml)
GROWN_UP_FILMS = {"Love Actually", "La La Land"}


@dataclass
class Case:
    label: str
    inputs: dict
    want_any: set[str] = field(default_factory=set)  # at least `want_count` of these in the six results
    want_count: int = 2
    never: set[str] = field(default_factory=set)  # none of these
    safety: bool = False  # kids cases: every result must pass the kids filter, and a failure fails the run
    known_weak: str = ""  # why titles from `never` still come back today; they count as weak picks, not a failure


CASES = [
    Case(
        "like Star Trek",
        dict(like=["Star Trek"]),
        {
            "Star Trek: Picard",
            "Star Trek",
            "Star Trek Beyond",
            "Star Trek Into Darkness",
            "The Expanse",
            "Stargate SG-1",
        },
        never={"Rocky", "The Brutalist", "The Vampire Diaries"},
    ),
    Case(
        "like Breaking Bad",
        dict(like=["Breaking Bad"]),
        {"Better Call Saul", "El Camino: A Breaking Bad Movie", "Narcos", "Narcos: Mexico", "Ozark"},
    ),
    Case(
        "like Suits",
        dict(like=["Suits"]),
        {
            "The Lincoln Lawyer",
            "Extraordinary Attorney Woo",
            "Goliath",
            "Beyond the Bar",
            "Partner Track",
            "The Good Wife",
            "Boston Legal",
        },
    ),
    Case(
        "series like Sherlock",
        dict(like=["Sherlock"], kind="tv"),
        {
            "Agatha Christie's Poirot",
            "The Mentalist",
            "A Good Girl's Guide to Murder",
            "Mouse",
            "Elementary",
            "The Residence",
            "Luther",
        },
    ),
    Case(
        "films like The Matrix",
        dict(like=["The Matrix"], kind="movie"),
        {"Blade Runner", "The Creator", "The Terminator", "The Electric State", "Inception"},
        never={"Step Up 2: The Streets"},
    ),
    Case(
        "like Interstellar",
        dict(like=["Interstellar"]),
        {"Project Hail Mary", "Stowaway", "Lost in Space", "Star Trek", "Gravity", "The Martian", "Ad Astra"},
    ),
    Case(
        "like Friends",
        dict(like=["Friends"]),
        {"Seinfeld", "Community", "Two and a Half Men", "The Office", "How I Met Your Mother", "New Girl"},
    ),
    Case(
        "like Game of Thrones",
        dict(like=["Game of Thrones"]),
        {
            "The Witcher",
            "The Witcher: Blood Origin",
            "The Lord of the Rings: The Rings of Power",
            "The Last Kingdom",
            "House of the Dragon",
            "Vikings",
        },
    ),
    Case(
        "like Spider-Man",
        dict(like=["Spider-Man"]),
        {
            "The Boys",
            "Marvel's Agents of S.H.I.E.L.D.",
            "Smallville",
            "Titans",
            "The Tick",
            "Watchmen",
            "Justice League",
        },
        3,
        never={"Top Gun", "You Only Live Twice"},
    ),
    Case(
        "like Ted Lasso",
        dict(like=["Ted Lasso"]),
        {
            "Space Force",
            "The Office",
            "Schitt's Creek",
            "Parks and Recreation",
            "Welcome to Wrexham",
            "Brooklyn Nine-Nine",
        },
        never={"The Devil Wears Prada", "Emily in Paris", "Letters to Juliet", "The Dilemma", "Mean Girls"},
        known_weak="matched on one keyword each: 'american abroad', 'fish out of water', 'male friendship'",
    ),
    Case(
        "like The Office",
        dict(like=["The Office"]),
        {
            "The Office",
            "30 Rock",
            "Parks and Recreation",
            "Superstore",
            "Brooklyn Nine-Nine",
            "Abbott Elementary",
            "Friends",
        },
        never={"Love Actually", "A Man Called Otto", "The Proposal"},
        known_weak="matched on one keyword each: 'office', 'remake', 'employer employee relationship'",
    ),
    Case(
        "something funny",
        dict(genres=["comedy"]),
        {
            "The Office",
            "Seinfeld",
            "Friends",
            "Brooklyn Nine-Nine",
            "Superstore",
            "The Good Place",
            "Community",
            "Modern Family",
        },
        never={"Parasite"},
        known_weak="TMDB tags Parasite Comedy; the genre discover has no tone signal",
    ),
    Case(
        "a sci-fi series",
        dict(genres=["sci-fi"], kind="tv"),
        {
            "The X-Files",
            "Stargate SG-1",
            "Person of Interest",
            "The Expanse",
            "Lost in Space",
            "Smallville",
            "The Handmaid's Tale",
            "The Walking Dead",
            "Foundation",
        },
        never={"The Vampire Diaries", "Supernatural"},
    ),
    Case(
        "a thriller series",
        dict(genres=["thriller"], kind="tv"),
        {
            "The Mentalist",
            "You",
            "Absentia",
            "Law & Order: Special Victims Unit",
            "Agatha Christie's Poirot",
            "Nine Perfect Strangers",
            "The Night Agent",
            "Mindhunter",
        },
    ),
    # safety: every result must pass the kids filter; the grown-up films are AL in NL cinemas but R / PG-13 in the US
    Case(
        "kids: calm film for a 5 year old",
        dict(genres=["animation", "family"], kind="movie", kids_age=5),
        {
            "Kiki's Delivery Service",
            "Ponyo",
            "The Gruffalo",
            "Beauty and the Beast",
            "Maya the Bee: The Honey Games",
            "My Neighbor Totoro",
            "Bluey",
        },
        never=GROWN_UP_FILMS,
        safety=True,
    ),
    # nothing is the right answer here (horror has no kids-safe titles on these services); a pure safety case
    Case(
        "kids: something scary, no age",
        dict(genres=["horror"], kids_mode=True),
        want_count=0,
        never=GROWN_UP_FILMS,
        safety=True,
    ),
    # the kids' catalogue holds one Trek, so one hit is the bar
    Case(
        "kids: like Star Trek for a 7 year old",
        dict(like=["Star Trek"], kids_age=7),
        {"Star Trek: Prodigy", "Pokémon", "Monsters vs. Aliens", "Monsters vs Aliens", "Howl's Moving Castle"},
        1,
        never=GROWN_UP_FILMS,
        safety=True,
    ),
]


async def run(cases: list[Case], cfg: config.Config, db: Path) -> int:
    services = services_mod.load(cfg)
    policy = cfg.kids_policy()
    cache = Cache(db)
    tmdb = Tmdb(cfg.tmdb_token, cache, cfg.ratings, cfg.timeouts.tmdb_s, ages=cfg.kids.ages,
                concurrency=cfg.server.lookup_concurrency)  # fmt: skip
    aliases = load_genre_aliases(INSTRUCTIONS_PATH.parent, cfg.language.code)
    genre_kw = load_genre_keywords(INSTRUCTIONS_PATH.parent, cfg.language.code)
    quality_pass = quality_total = weak_picks = 0
    safety_failed = False
    for case in cases:
        cards = await find_titles(
            tmdb, PickRegistry(), set(), services, policy, limit=6, concurrency=cfg.server.lookup_concurrency,
            candidates=cfg.answers.candidates, hours_candidates=cfg.answers.hours_candidates, genre_aliases=aliases,
            genre_keywords=genre_kw, ranking=cfg.ranking, **case.inputs,
        )  # fmt: skip
        titles = [c.title for c in cards]
        problems, weak = [], []
        if not cards and not case.safety:  # for kids, nothing is a safe answer (Claude then says so)
            problems.append("no titles")
        if case.want_any and len(set(titles) & case.want_any) < case.want_count:
            problems.append(f"wanted {case.want_count} of {sorted(case.want_any)}")
        if bad := set(titles) & case.never:
            if case.known_weak and not case.safety:
                weak = sorted(bad)
            else:
                problems.append(f"must not show {sorted(bad)}")
        if case.safety:
            kids_age = case.inputs.get("kids_age")
            if unsafe := [c.title for c in cards if not allowed(c.certification, kids_age, True, policy)]:
                problems.append(f"not allowed for kids: {unsafe}")
            safety_failed |= bool(problems)
        else:
            quality_total += 1
            quality_pass += not problems
        weak_picks += len(weak)
        print(f"{'FAIL' if problems else 'weak' if weak else 'ok  '} {case.label}: {', '.join(titles) or '-'}")
        for prob in problems:
            print(f"       {prob}")
        if weak:
            print(f"       weak picks {weak} (known weak: {case.known_weak})")
    await tmdb.aclose()
    cache.close()
    share = quality_pass / quality_total if quality_total else 1.0
    known = sum(1 for c in cases if c.known_weak)
    print(
        f"\nregression snapshot: quality {quality_pass}/{quality_total} ({share:.0%}, pass mark {PASS_MARK:.0%}); "
        f"{weak_picks} weak picks across {known} known-weak cases; safety {'FAILED' if safety_failed else 'ok'}"
    )
    return 0 if share >= PASS_MARK and not safety_failed else 1


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, default=ROOT / "lumitv.example.toml")
    p.add_argument("--db", type=Path, default=ROOT / ".tmp" / "like_eval.db")
    p.add_argument("--fresh", action="store_true", help="throwaway cache: every lookup goes to TMDB (~40 s)")
    args = p.parse_args()
    cfg = config.load(args.config, env_file=ROOT / ".env")
    if args.fresh:
        with tempfile.TemporaryDirectory() as tmp:
            return await run(CASES, cfg, Path(tmp) / "quality.db")
    args.db.parent.mkdir(parents=True, exist_ok=True)
    return await run(CASES, cfg, args.db)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
