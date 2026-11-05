"""Put the household tables (watch history, feedback, opened titles) back from a nightly backup.

    uv run python scripts/restore_household.py backups/household-2026-11-04.db [--db lumitv.db]

Stop the server first (deploy/windows.md, Stop and restart). The TMDB cache in the same file is left alone.
"""

import argparse
from pathlib import Path

from lumitv.store import restore


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("backup", type=Path)
    p.add_argument("--db", type=Path, default=Path("lumitv.db"))
    args = p.parse_args()
    if not args.backup.exists():
        raise SystemExit(f"no such backup: {args.backup}")
    for table, n in restore(args.db, args.backup).items():
        print(f"{table}: {n} rows")


if __name__ == "__main__":
    main()
