"""Search routes — the query API for the server-side search engine (kinora.md §8).

The REST surface over :mod:`app.search` (BM25 + pgvector hybrid, RRF-fused), kept
distinct from any client-side discovery UI:

* ``GET  /search`` — run a query (free text + filters + facets + highlighting),
  scoped to the caller's owned books unless a ``book_id`` they own is given.
* ``GET  /search/suggest`` — autocomplete a prefix (typo-tolerant).
* ``POST /search/reindex`` — (re)index one of the caller's books incrementally.

Library-wide search is scoped to the caller's books (fail-closed): the gateway
resolves the owned book-id set and constrains the query so a reader never sees
another user's content. A single ``book_id`` filter must be a book they own.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.core.logging import get_logger
from app.db.repositories.book import BookRepo
from app.search.documents import DocKind
from app.search.index import Facet, SearchHit, SearchMode
from app.search.service import SearchResult, SearchService

logger = get_logger("app.api.search")

router = APIRouter(prefix="/search", tags=["search"])


# --------------------------------------------------------------------------- #
# Response schemas (route-local; the route owns its own contract)
# --------------------------------------------------------------------------- #


class HighlightView(BaseModel):
    """A highlighted snippet for one matched field."""

    field: str
    snippet: str


class SearchHitView(BaseModel):
    """One ranked search result projected for the API."""

    doc_id: str
    kind: str
    ref_id: str
    book_id: str | None = None
    score: float
    title: str
    highlights: list[HighlightView] = Field(default_factory=list)
    lexical_rank: int | None = None
    semantic_rank: int | None = None
    payload: dict[str, object] = Field(default_factory=dict)


class FacetCountView(BaseModel):
    """One facet value + its matching-document count."""

    value: str
    count: int


class FacetView(BaseModel):
    """A facet field and its top value counts."""

    field: str
    counts: list[FacetCountView]


class SearchResponseView(BaseModel):
    """The query result page: hits, total, facets, mode, timing, suggestion."""

    query: str
    mode: str
    total: int
    took_ms: float
    hits: list[SearchHitView]
    facets: list[FacetView] = Field(default_factory=list)
    suggestion: str | None = None


class SuggestResponse(BaseModel):
    """Autocomplete suggestions for a prefix."""

    prefix: str
    suggestions: list[str]


class ReindexResponse(BaseModel):
    """The outcome of a (re)index pass for one book."""

    book_id: str
    indexed: int
    by_kind: dict[str, int]
    index_version: str


# --------------------------------------------------------------------------- #
# Projection helpers
# --------------------------------------------------------------------------- #


def _hit_view(hit: SearchHit) -> SearchHitView:
    return SearchHitView(
        doc_id=hit.doc_id,
        kind=hit.kind.value,
        ref_id=hit.ref_id,
        book_id=hit.book_id,
        score=hit.score,
        title=hit.title,
        highlights=[HighlightView(field=h.field, snippet=h.snippet) for h in hit.highlights],
        lexical_rank=hit.lexical_rank,
        semantic_rank=hit.semantic_rank,
        payload=hit.payload,
    )


def _facet_view(facet: Facet) -> FacetView:
    return FacetView(
        field=facet.field,
        counts=[FacetCountView(value=c.value, count=c.count) for c in facet.counts],
    )


def _parse_kinds(kinds: list[str] | None) -> list[DocKind] | None:
    if not kinds:
        return None
    out: list[DocKind] = []
    for raw in kinds:
        try:
            out.append(DocKind(raw))
        except ValueError as exc:
            raise APIError("invalid_kind", f"unknown doc kind: {raw}", status=422) from exc
    return out


def _parse_mode(mode: str) -> SearchMode:
    try:
        return SearchMode(mode)
    except ValueError as exc:
        raise APIError(
            "invalid_mode", "mode must be lexical|semantic|hybrid", status=422
        ) from exc


async def _owned_book_ids(container: ContainerDep, user_id: str) -> set[str]:
    async with container.session_factory() as session:
        books = await BookRepo(session).list_for_user(user_id)
    return {b.id for b in books}


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("", response_model=SearchResponseView)
async def search(
    container: ContainerDep,
    user: CurrentUser,
    q: Annotated[str, Query(description="The query string")] = "",
    mode: Annotated[str, Query()] = "hybrid",
    book_id: Annotated[str | None, Query()] = None,
    kind: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    highlight: Annotated[bool, Query()] = True,
) -> SearchResponseView:
    """Run a search scoped to the caller's owned books (kinora.md §8 projection)."""
    owned = await _owned_book_ids(container, user.id)
    if book_id is not None and book_id not in owned:
        raise APIError("book_not_found", "no such book for this user", status=404)

    search_mode = _parse_mode(mode)
    kinds = _parse_kinds(kind)
    limit = min(limit, container.settings.search_max_limit)

    service = SearchService(
        await container.resolve_search_index(), embedder=container._embedder()  # noqa: SLF001
    )

    # Scope: a single owned book, or the union of the caller's books. When the
    # user owns no books the result is trivially empty (fail-closed).
    scoped_book = book_id
    if scoped_book is None and not owned:
        return SearchResponseView(
            query=q, mode=search_mode.value, total=0, took_ms=0.0, hits=[]
        )

    result = await _run_scoped_search(
        service,
        q,
        mode=search_mode,
        owned=owned,
        scoped_book=scoped_book,
        kinds=kinds,
        limit=limit,
        offset=offset,
        highlight=highlight,
        container=container,
    )
    resp = result.response
    return SearchResponseView(
        query=q,
        mode=resp.mode.value,
        total=resp.total,
        took_ms=resp.took_ms,
        hits=[_hit_view(h) for h in resp.hits],
        facets=[_facet_view(f) for f in resp.facets],
        suggestion=result.suggestion,
    )


async def _run_scoped_search(
    service: SearchService,
    q: str,
    *,
    mode: SearchMode,
    owned: set[str],
    scoped_book: str | None,
    kinds: list[DocKind] | None,
    limit: int,
    offset: int,
    highlight: bool,
    container: ContainerDep,
) -> SearchResult:
    """Run a single-book or library-wide search with per-user scoping.

    A single ``scoped_book`` pushes the scope into the index (cheap). A
    library-wide search over many owned books filters the index result to the
    owned set (the index has no per-user partition yet; this is the fail-closed
    bridge until §"per-user partitions" lands).
    """
    if scoped_book is not None:
        return await service.search(
            q,
            mode=mode,
            limit=limit,
            offset=offset,
            book_id=scoped_book,
            kinds=kinds,
            highlight=highlight,
            rrf_k=container.settings.search_rrf_k,
            lexical_weight=container.settings.search_lexical_weight,
            semantic_weight=container.settings.search_semantic_weight,
        )
    # Library-wide: over-fetch then filter to owned, paginate in Python.
    result = await service.search(
        q,
        mode=mode,
        limit=limit + offset + 50,
        offset=0,
        book_id=None,
        kinds=kinds,
        highlight=highlight,
        rrf_k=container.settings.search_rrf_k,
        lexical_weight=container.settings.search_lexical_weight,
        semantic_weight=container.settings.search_semantic_weight,
        suggest_on_thin=False,
    )
    owned_hits = [h for h in result.response.hits if h.book_id in owned]
    page = owned_hits[offset : offset + limit]
    # Rebuild a response carrying only the owned page.
    scoped_response = replace(result.response, hits=page, total=len(owned_hits))
    return SearchResult(response=scoped_response, suggestion=result.suggestion)


@router.get("/suggest", response_model=SuggestResponse)
async def suggest(
    container: ContainerDep,
    user: CurrentUser,
    q: Annotated[str, Query(description="The prefix to autocomplete")],
    limit: Annotated[int, Query(ge=1, le=25)] = 8,
) -> SuggestResponse:
    """Typo-tolerant autocomplete over the indexed vocabulary."""
    service = SearchService(
        await container.resolve_search_index(), embedder=container._embedder()  # noqa: SLF001
    )
    suggestions = await service.suggest(q, limit=limit)
    return SuggestResponse(prefix=q, suggestions=suggestions)


@router.post(
    "/reindex/{book_id}",
    response_model=ReindexResponse,
    dependencies=[Depends(write_rate_limit)],
)
async def reindex_book(
    book_id: str, container: ContainerDep, user: CurrentUser
) -> ReindexResponse:
    """Incrementally (re)index one of the caller's books into the live index."""
    owned = await _owned_book_ids(container, user.id)
    if book_id not in owned:
        raise APIError("book_not_found", "no such book for this user", status=404)
    index = await container.resolve_search_index()
    ensure = getattr(index, "ensure_schema", None)
    if ensure is not None:
        await ensure()
    pipeline = container.build_search_pipeline()
    stats = await pipeline.index_book(book_id, index)
    return ReindexResponse(
        book_id=book_id,
        indexed=stats.total,
        by_kind=stats.by_kind,
        index_version=stats.index_version,
    )


__all__ = ["router"]
