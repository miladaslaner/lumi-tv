"""Import Netflix 'Viewing activity -> Download all' CSV into the family store."""

import csv
from pathlib import Path

from lumitv.store import Store

_SEPARATORS = (": Season ", ": Limited Series", ": Part ", ": Volume ", ": Chapter ")


def show_name(title: str) -> str:
    cuts = [i for sep in _SEPARATORS if (i := title.find(sep)) != -1]
    return title[: min(cuts)] if cuts else title


def parse_raw(path: str | Path) -> list[tuple[str, str, str]]:
    """Return (show_or_film, date, raw_title) per row; the date string is kept as given."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [
            (show_name(raw), row["Date"].strip(), raw)
            for row in csv.DictReader(f)
            if row.get("Title") and row.get("Date")
            for raw in [row["Title"].strip()]
        ]


def parse(path: str | Path) -> list[tuple[str, str]]:
    """Return (show_or_film, date) per row."""
    return [(show, date) for show, date, _ in parse_raw(path)]


def import_csv(store: Store, path: str | Path) -> int:
    """Add rows to the store; returns how many were new."""
    return store.add_watched(parse_raw(path), source="netflix")
