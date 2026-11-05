from typing import Protocol

from pydantic import BaseModel

from .models import Kind


class TmdbUnavailable(Exception):
    """The catalog could not be reached and nothing usable was cached."""


class Details(BaseModel):
    title: str
    year: int | None
    overview: str
    poster_url: str | None
    certification: str | None
    adult: bool = False  # the source's adult flag: such a title is never a candidate, in any mode
    runtime_minutes: int | None  # movies
    seasons: int | None  # series
    season1_minutes: int | None  # series
    season2_minutes: int | None = None  # series with a second season
    total_minutes: int | None  # series
    flatrate_by_region: dict[str, set[int]]
    genre_ids: list[int] = []
    keywords: dict[int, str] = {}  # TMDB keyword id -> name ("space opera"): what "like X" matches on


class CatalogSource(Protocol):
    async def search(self, query: str, kind: Kind | None) -> list[dict]: ...

    async def similar(self, tmdb_id: int, kind: Kind) -> list[dict]: ...

    async def discover(
        self,
        kind: Kind,
        region: str,
        provider_ids: set[int],
        genres: list[int] | None,
        certification_lte: str | None,
        page: int = 1,
        any_genre: bool = False,
        min_votes: int | None = None,
        keywords: list[int] | None = None,
    ) -> list[dict]:
        """`genres` must all match, or with any_genre at least one; `min_votes` drops thinly rated titles;
        `keywords`: titles with at least one of them."""
        ...

    async def genres(self, kind: Kind) -> dict[str, int]:
        """The source's genre names (English) for one kind, name -> id."""
        ...

    async def details(self, tmdb_id: int, kind: Kind, exact_minutes: bool = False) -> Details: ...

    async def keyword_ids(self, names: list[str]) -> dict[str, int]:
        """TMDB keyword ids for keyword names (exact name matches; unknown names are left out)."""
        ...

    async def keyword_count(self, keyword_id: int) -> int:
        """How many titles (films and series, anywhere) carry a keyword: rare keywords say more about a title."""
        ...
