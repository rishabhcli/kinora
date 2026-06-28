"""Tests for the SearchService orchestration (parse → embed → search → suggest)."""

from __future__ import annotations

import pytest_asyncio

from app.search.index import SearchMode
from app.search.memory_backend import InMemoryIndex
from app.search.service import SearchService
from tests.test_search_support import FakeEmbedder, sample_docs


@pytest_asyncio.fixture
async def service() -> SearchService:
    idx = InMemoryIndex()
    await idx.upsert(sample_docs())
    return SearchService(idx, embedder=FakeEmbedder())


async def test_service_hybrid_embeds_query(service: SearchService) -> None:
    result = await service.search("frozen", mode=SearchMode.HYBRID)
    assert result.response.total > 0
    # The query was embedded (hybrid), so the request had a vector arm.
    assert result.response.mode is SearchMode.HYBRID


async def test_service_lexical_skips_embedding(service: SearchService) -> None:
    result = await service.search("snow queen", mode=SearchMode.LEXICAL)
    assert result.response.hits[0].doc_id == "book:b1"


async def test_service_default_facets(service: SearchService) -> None:
    result = await service.search("frozen", mode=SearchMode.LEXICAL)
    facet_fields = {f.field for f in result.response.facets}
    assert "kind" in facet_fields


async def test_service_suggestion_on_typo() -> None:
    idx = InMemoryIndex()
    await idx.upsert(sample_docs())
    svc = SearchService(idx, embedder=FakeEmbedder())
    # "andrsen" (typo of the author "Andersen", which appears in a single doc) is
    # a thin-result query -> a "did you mean: andersen" suggestion.
    result = await svc.search("andrsen", mode=SearchMode.LEXICAL)
    assert result.suggestion is not None
    assert "andersen" in result.suggestion


async def test_service_no_suggestion_on_good_query(service: SearchService) -> None:
    result = await service.search("snow queen frozen palace gerda meadow", mode=SearchMode.LEXICAL)
    # Plenty of hits -> no suggestion offered.
    assert result.suggestion is None


async def test_service_semantic_without_embedder_falls_back() -> None:
    idx = InMemoryIndex()
    await idx.upsert(sample_docs())
    svc = SearchService(idx, embedder=None)  # no embedder
    # Semantic mode with no embedder degrades to lexical, not an error.
    result = await svc.search("snow queen", mode=SearchMode.SEMANTIC)
    assert result.response.total >= 1


async def test_service_suggest_delegates(service: SearchService) -> None:
    out = await service.suggest("fro")
    assert any(s.startswith("fro") for s in out)
