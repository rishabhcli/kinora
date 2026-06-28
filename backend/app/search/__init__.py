"""Backend search & indexing service (kinora.md §8 — search complements the canon).

A server-side corpus search engine over the library: books, pages, scenes,
beats, canon entities, and shots are projected into :class:`SearchDocument`s and
indexed for BM25 + dense-vector hybrid retrieval (reciprocal-rank fusion), with
query parsing (phrase / boolean / field filters / facets / ranges), highlighting,
typo tolerance, synonyms, facet aggregation, and versioned-alias bulk reindex.

The engine is *pluggable* behind :class:`~app.search.index.SearchIndex`:

* :class:`~app.search.memory_backend.InMemoryIndex` — full engine, no infra
  (tests + offline);
* :class:`~app.search.postgres_backend.PostgresIndex` — Postgres FTS + pgvector.

This is distinct from any client-side discovery UI and from the per-beat canon
retrieval policy in :mod:`app.memory.retrieval` (which it reuses but does not
duplicate).
"""

from __future__ import annotations

from app.search.documents import DocKind, SearchDocument, make_doc_id
from app.search.index import (
    Facet,
    FacetCount,
    SearchHit,
    SearchIndex,
    SearchMode,
    SearchRequest,
    SearchResponse,
)
from app.search.memory_backend import InMemoryIndex
from app.search.query import ParsedQuery, parse_query

__all__ = [
    "DocKind",
    "Facet",
    "FacetCount",
    "InMemoryIndex",
    "ParsedQuery",
    "SearchDocument",
    "SearchHit",
    "SearchIndex",
    "SearchMode",
    "SearchRequest",
    "SearchResponse",
    "make_doc_id",
    "parse_query",
]
