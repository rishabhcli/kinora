"""The search service — the orchestration layer over an index backend.

Ties together the pieces a single query needs:

    raw string + filters
      → parse_query (structured ParsedQuery)
      → embed the free-text (the dense arm's query vector, via the Embedder)
      → index.search (BM25 + vector, fused by RRF, facets, highlighting)
      → "did you mean" suggestion when the result is thin

It is backend-agnostic: hand it any :class:`~app.search.index.SearchIndex`
(in-memory or Postgres) and it behaves identically. The query embedding is
best-effort — a failure (or no embedder) simply falls back to lexical-only,
never an error. Embeddings always go through the :class:`Embedder` protocol, so
tests inject a fake embedder and the service never hits the network.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.core.logging import get_logger
from app.memory.interfaces import Embedder
from app.search.analyzer import Analyzer, auto_fuzziness, default_analyzer, within_distance
from app.search.documents import DocKind
from app.search.index import (
    SearchIndex,
    SearchMode,
    SearchRequest,
    SearchResponse,
)
from app.search.query import parse_query

logger = get_logger("app.search.service")

#: The default facet fields aggregated when the caller doesn't specify any.
DEFAULT_FACETS: tuple[str, ...] = ("kind", "entity_type", "status", "render_mode")

#: Below this many hits we try a typo-corrected suggestion ("did you mean").
_SUGGEST_THRESHOLD = 3


@dataclass(frozen=True)
class SearchResult:
    """A search response plus an optional spelling suggestion."""

    response: SearchResponse
    suggestion: str | None = None


class SearchService:
    """Parse → embed → search → suggest, over a pluggable index backend."""

    def __init__(
        self,
        index: SearchIndex,
        *,
        embedder: Embedder | None = None,
        analyzer: Analyzer | None = None,
    ) -> None:
        self._index = index
        self._embedder = embedder
        self._analyzer = analyzer or default_analyzer()

    @property
    def index(self) -> SearchIndex:
        """The underlying index backend (for the pipeline / admin routes)."""
        return self._index

    async def search(
        self,
        query: str,
        *,
        mode: SearchMode = SearchMode.HYBRID,
        limit: int = 20,
        offset: int = 0,
        book_id: str | None = None,
        kinds: Sequence[DocKind] | None = None,
        facet_fields: Sequence[str] | None = None,
        highlight: bool = True,
        rrf_k: int = 60,
        lexical_weight: float = 1.0,
        semantic_weight: float = 1.0,
        suggest_on_thin: bool = True,
    ) -> SearchResult:
        """Run a full query and return the response (+ optional suggestion)."""
        parsed = parse_query(query)
        query_embedding: Sequence[float] | None = None
        if mode is not SearchMode.LEXICAL and parsed.has_text:
            query_embedding = await self._embed_query(parsed.free_text)
            if query_embedding is None and mode is SearchMode.SEMANTIC:
                # No vector available -> there is nothing for the semantic arm.
                mode = SearchMode.LEXICAL

        request = SearchRequest(
            query=parsed,
            mode=mode,
            limit=limit,
            offset=offset,
            book_id=book_id,
            kinds=tuple(kinds) if kinds else None,
            facet_fields=tuple(facet_fields) if facet_fields is not None else DEFAULT_FACETS,
            highlight=highlight,
            query_embedding=query_embedding,
            rrf_k=rrf_k,
            lexical_weight=lexical_weight,
            semantic_weight=semantic_weight,
        )
        response = await self._index.search(request)

        suggestion: str | None = None
        # Offer a "did you mean" when a query term looks misspelled — either the
        # result set is thin, or a term has no exact vocabulary match (so it only
        # matched fuzzily, if at all). Both are signals the user mistyped.
        if suggest_on_thin and parsed.has_text and response.total < _SUGGEST_THRESHOLD:
            suggestion = await self._did_you_mean(parsed.positive_terms)
        return SearchResult(response=response, suggestion=suggestion)

    async def suggest(self, prefix: str, *, limit: int = 8) -> list[str]:
        """Autocomplete terms for a prefix (delegates to the backend)."""
        return await self._index.suggest(prefix, limit=limit)

    async def _embed_query(self, text: str) -> list[float] | None:
        if self._embedder is None or not text.strip():
            return None
        try:
            vectors = await self._embedder.embed_texts([text])
        except Exception as exc:  # noqa: BLE001 - degrade to lexical-only
            logger.warning("search.query_embed_failed", error=str(exc))
            return None
        return vectors[0] if vectors else None

    async def _did_you_mean(self, terms: Sequence[str]) -> str | None:
        """Suggest a typo correction for the first low-recall term, if any.

        For each query term, ask the backend for vocabulary suggestions; if the
        closest one is within the term's fuzziness budget but *not* the term
        itself, propose it. Returns the query with that one term replaced.
        """
        for i, term in enumerate(terms):
            analyzed = self._analyzer.analyze(term)
            base = analyzed[0] if analyzed else term.lower()
            budget = auto_fuzziness(base)
            if budget == 0:
                continue
            candidates = await self._index.suggest(term, limit=5)
            for cand in candidates:
                if cand != base and within_distance(base, cand, budget):
                    corrected = list(terms)
                    corrected[i] = cand
                    return " ".join(corrected)
        return None


__all__ = ["DEFAULT_FACETS", "SearchResult", "SearchService"]
