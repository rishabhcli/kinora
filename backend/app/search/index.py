"""The pluggable search-index abstraction — one protocol, many backends.

:class:`SearchIndex` is the seam the rest of the subsystem (service, pipeline,
route) depends on. Two real implementations satisfy it:

* :class:`~app.search.memory_backend.InMemoryIndex` — a full BM25 + cosine + RRF
  engine with no infrastructure (used by tests and any offline path);
* :class:`~app.search.postgres_backend.PostgresIndex` — Postgres FTS + pgvector
  hybrid over the ``search_documents`` table (the production backend).

Both consume the same :class:`~app.search.query.ParsedQuery` and return the same
:class:`SearchResponse`, so the service is backend-agnostic and a test can run
the real ranking logic without a database.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.search.documents import DocKind, SearchDocument
from app.search.query import ParsedQuery


class SearchMode(enum.StrEnum):
    """Which retrieval arms to use for a query."""

    LEXICAL = "lexical"  # BM25 / FTS only
    SEMANTIC = "semantic"  # dense vector only
    HYBRID = "hybrid"  # both arms fused with RRF


@dataclass(frozen=True)
class FieldHighlight:
    """A highlighted snippet for one matched field."""

    field: str
    snippet: str


@dataclass(frozen=True)
class SearchHit:
    """One ranked result: the document, its fused score, and per-field highlights."""

    doc_id: str
    kind: DocKind
    ref_id: str
    book_id: str | None
    score: float
    title: str
    highlights: list[FieldHighlight] = field(default_factory=list)
    lexical_rank: int | None = None
    semantic_rank: int | None = None
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class FacetCount:
    """One value of a facet and how many matching docs carry it."""

    value: str
    count: int


@dataclass(frozen=True)
class Facet:
    """A facet field and its top value counts (descending by count)."""

    field: str
    counts: list[FacetCount]


@dataclass(frozen=True)
class SearchRequest:
    """A fully-structured search request the index executes."""

    query: ParsedQuery
    mode: SearchMode = SearchMode.HYBRID
    limit: int = 20
    offset: int = 0
    book_id: str | None = None  # hard scope to one book
    kinds: tuple[DocKind, ...] | None = None  # hard scope to doc kinds
    facet_fields: tuple[str, ...] = ()  # which fields to aggregate counts for
    highlight: bool = True
    query_embedding: Sequence[float] | None = None  # the dense-arm query vector
    rrf_k: int = 60
    lexical_weight: float = 1.0
    semantic_weight: float = 1.0


@dataclass(frozen=True)
class SearchResponse:
    """The result of a search: the page of hits, total, facets, and timing."""

    hits: list[SearchHit]
    total: int
    facets: list[Facet] = field(default_factory=list)
    took_ms: float = 0.0
    mode: SearchMode = SearchMode.HYBRID


@runtime_checkable
class SearchIndex(Protocol):
    """The contract every search backend satisfies (kinora.md §8 projection)."""

    async def upsert(self, documents: Iterable[SearchDocument]) -> int:
        """Insert-or-replace documents by ``doc_id``; return the count written."""
        ...

    async def delete(self, doc_ids: Iterable[str]) -> int:
        """Remove documents by id; return the count deleted."""
        ...

    async def delete_by_book(self, book_id: str) -> int:
        """Remove every document belonging to a book; return the count deleted."""
        ...

    async def search(self, request: SearchRequest) -> SearchResponse:
        """Execute a structured search and return the ranked page + facets."""
        ...

    async def suggest(self, prefix: str, *, limit: int = 8) -> list[str]:
        """Autocomplete: terms in the index sharing ``prefix`` (typo-tolerant)."""
        ...

    async def count(self, *, book_id: str | None = None) -> int:
        """Number of documents in the index (optionally scoped to a book)."""
        ...

    async def clear(self) -> None:
        """Drop every document (used by a full bulk reindex into this version)."""
        ...


__all__ = [
    "Facet",
    "FacetCount",
    "FieldHighlight",
    "SearchHit",
    "SearchIndex",
    "SearchMode",
    "SearchRequest",
    "SearchResponse",
]
