"""Print TMDB's watch providers (id and name) per region, to fill [[services]] tmdb_provider_ids in lumitv.toml.

    uv run python scripts/list_providers.py [REGION ...]        e.g. US GB NL; default: the example config's regions

One line per provider: region, id, name (tab-separated). Reads TMDB_TOKEN from .env next to this project or from
the environment; the token is never printed.
"""

import argparse
import os
import sys
import tomllib
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


def default_regions() -> list[str]:
    """The regions the example config's services use, sorted."""
    with (ROOT / "lumitv.example.toml").open("rb") as f:
        return sorted({s["region"] for s in tomllib.load(f)["services"] if s["region"]})


def providers(client: httpx.Client, region: str) -> dict[int, str]:
    """Every provider TMDB lists for `region`, films and series together: id -> name."""
    found: dict[int, str] = {}
    for kind in ("movie", "tv"):
        r = client.get(f"/watch/providers/{kind}", params={"watch_region": region})
        r.raise_for_status()
        for p in r.json()["results"]:
            found[p["provider_id"]] = p["provider_name"]
    return found


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("regions", nargs="*", metavar="REGION", help="ISO 3166-1 country code, e.g. US")
    args = p.parse_args(argv)
    token = {**dotenv_values(ROOT / ".env"), **os.environ}.get("TMDB_TOKEN")
    if not token:
        print("TMDB_TOKEN missing (.env or environment)", file=sys.stderr)
        return 2
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url="https://api.themoviedb.org/3", headers=headers, timeout=10) as client:
        for region in args.regions or default_regions():
            for pid, name in sorted(providers(client, region).items()):
                print(f"{region}\t{pid}\t{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
