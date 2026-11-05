"""Real check for exact-title opening: each title's Google id (Google TV page URL) and Netflix id.

Run from lumi-tv/server:
    uv run python scripts/title_ids_check.py [--config lumitv.example.toml] [title ...]
Needs TMDB_TOKEN in .env next to the config (never printed). Uses a fresh temp cache DB, so timings are cold,
then repeats once to show the cache. Also says whether each title is on the configured Netflix region.
"""

import argparse
import asyncio
import tempfile
import time
from pathlib import Path

from lumitv import config, services as services_mod
from lumitv.services import google_tv_url
from lumitv.title_ids import TitleIdResolver, Wikidata
from lumitv.tmdb import Cache, Tmdb

ROOT = Path(__file__).resolve().parents[1]
TITLES = ["Upload", "Suits", "The Boys", "Paddington 2", "The Librarians"]
ALL_IDS = frozenset({"google_id", "netflix_id"})


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "lumitv.example.toml"))
    ap.add_argument("titles", nargs="*", default=TITLES)
    ap.add_argument(
        "--netflix-sample", type=int, default=0, help="also: Netflix id coverage of N popular Netflix titles"
    )
    args = ap.parse_args()
    cfg = config.load(args.config, env_file=ROOT / ".env")
    services = services_mod.load(cfg)
    netflix = services.get("netflix")
    with tempfile.TemporaryDirectory() as tmp:
        cache = Cache(Path(tmp) / "c.db")
        tmdb = Tmdb(cfg.tmdb_token, cache, cfg.ratings, cfg.timeouts.tmdb_s)
        wikidata = Wikidata(cache, cfg.timeouts.wikidata_s)
        resolver = TitleIdResolver(tmdb, wikidata, services, cfg.open.title_id_wait_s)
        try:
            rows = []
            for name in args.titles:
                hits = await tmdb.search(name, None)
                if not hits:
                    print(f"{name}: not found on TMDB")
                    continue
                hit = hits[0]
                kind, tmdb_id = hit["media_type"], hit["id"]
                ext = await tmdb.external_ids(kind, tmdb_id)
                t0 = time.perf_counter()
                ids = await resolver.resolve(kind, tmdb_id, ALL_IDS)
                cold_ms = (time.perf_counter() - t0) * 1000
                t0 = time.perf_counter()
                await resolver.resolve(kind, tmdb_id, ALL_IDS)
                warm_ms = (time.perf_counter() - t0) * 1000
                details = await tmdb.details(tmdb_id, kind)
                on_netflix = bool(
                    netflix and netflix.tmdb_provider_ids & details.flatrate_by_region.get(netflix.region, set())
                )
                gid = ids.get("google_id")
                source = "tmdb freebase_mid" if gid and gid == ext.get("freebase_mid") else ("wikidata" if gid else "-")
                wd, nf_id, region = ext.get("wikidata_id"), ids.get("netflix_id"), netflix.region if netflix else "-"
                rows.append((name, kind, tmdb_id, wd, gid, source, nf_id, on_netflix))
                print(
                    f"{name} ({kind} {tmdb_id}, {wd}): google_id={gid} via {source}"
                    f" netflix_id={nf_id} on_netflix_{region}={on_netflix}"
                    f" cold={cold_ms:.0f}ms cached={warm_ms:.1f}ms"
                )
                if gid:
                    print("   ", google_tv_url(cfg.open.google_tv_url, gid))
            on_nf = [r for r in rows if r[7]]
            print(f"\nP1874 (Netflix id) coverage of titles on Netflix: {sum(1 for r in on_nf if r[6])}/{len(on_nf)}")
            print(f"Google id found: {sum(1 for r in rows if r[4])}/{len(rows)}")
            if args.netflix_sample and netflix:
                popular = []
                providers = set(netflix.tmdb_provider_ids)
                for kind in ("movie", "tv"):
                    for page in (1, 2):
                        rows_ = await tmdb.discover(kind, netflix.region, providers, None, None, page=page)
                        popular += [(kind, r["id"]) for r in rows_]
                sample = popular[: args.netflix_sample]
                found = await asyncio.gather(*(resolver.resolve(k, i, ALL_IDS) for k, i in sample))
                n_nf = sum(1 for f in found if "netflix_id" in f)
                n_g = sum(1 for f in found if "google_id" in f)
                print(
                    f"Popular {netflix.region} Netflix sample ({len(sample)}, movies then series): "
                    f"netflix_id {n_nf}/{len(sample)}, google_id {n_g}/{len(sample)}"
                )
        finally:
            await tmdb.aclose()
            await wikidata.aclose()


if __name__ == "__main__":
    asyncio.run(main())
