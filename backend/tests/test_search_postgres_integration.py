"""Integration tests for the Postgres search backend (FTS + pgvector hybrid).

Requires an isolated throwaway Postgres with the ``vector`` extension. Set
``KINORA_SEARCH_TEST_DATABASE_URL`` to an isolated DB (never the live ``kinora``
DB). The suite creates the schema with ``create_all`` + the backend's idempotent
``ensure_schema`` DDL (the generated tsvector column + GIN/HNSW indexes), then
exercises the real FTS / vector / RRF path. Skips cleanly when no DB is set.

Run example::

    KINORA_SEARCH_TEST_DATABASE_URL=postgresql+asyncpg://kinora:kinora@localhost:5433/search_test \\
        backend/.venv/bin/pytest tests/test_search_postgres_integration.py -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401 - register tables on Base.metadata
from app.db.base import Base
from app.search.alias import InMemoryAliasRegistry, new_version
from app.search.index import SearchMode, SearchRequest
from app.search.pipeline import IndexingPipeline
from app.search.postgres_backend import PostgresIndex
from app.search.query import parse_query
from tests.test_search_support import FakeEmbedder, sample_docs

_DB_URL = (
    os.environ.get("KINORA_SEARCH_TEST_DATABASE_URL")
    or os.environ.get("KINORA_TEST_DATABASE_URL")
)

requires_db = pytest.mark.skipif(
    not _DB_URL,
    reason="Postgres search tests require KINORA_SEARCH_TEST_DATABASE_URL (isolated DB)",
)

pytestmark = requires_db


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[object]:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        # Clean slate for an isolated test DB: search tables + the source rows the
        # reindex test seeds (books cascades to pages/scenes/beats/entities/shots).
        await conn.execute(
            text("TRUNCATE search_documents, search_index_aliases, books CASCADE")
        )
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def factory() -> AsyncIterator[object]:
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def pg_index(session_factory: object) -> PostgresIndex:
    idx = PostgresIndex(session_factory, index_version="v1")
    await idx.ensure_schema()
    embedder = FakeEmbedder()
    docs = sample_docs()
    for d in docs:
        if d.embedding is None and d.all_text():
            d.embedding = (await embedder.embed_texts([d.all_text()]))[0]
    await idx.upsert(docs)
    return idx


async def _ids(idx: PostgresIndex, query: str, **kwargs: object) -> list[str]:
    req = SearchRequest(query=parse_query(query), **kwargs)  # type: ignore[arg-type]
    resp = await idx.search(req)
    return [h.doc_id for h in resp.hits]


async def test_fts_title_match(pg_index: PostgresIndex) -> None:
    hits = await _ids(pg_index, "snow queen", mode=SearchMode.LEXICAL)
    assert "book:b1" in hits


async def test_fts_body_match(pg_index: PostgresIndex) -> None:
    hits = await _ids(pg_index, "frozen forest", mode=SearchMode.LEXICAL)
    assert "page:p1" in hits


async def test_fts_stemming(pg_index: PostgresIndex) -> None:
    # Postgres english config stems "running" -> matches "runs".
    hits = await _ids(pg_index, "running", mode=SearchMode.LEXICAL)
    assert "beat:bt1" in hits


async def test_fts_phrase(pg_index: PostgresIndex) -> None:
    hits = await _ids(pg_index, '"ice palace"', mode=SearchMode.LEXICAL)
    assert "beat:bt1" in hits


async def test_fts_must_not(pg_index: PostgresIndex) -> None:
    hits = await _ids(pg_index, "frozen -forest", mode=SearchMode.LEXICAL)
    assert "page:p1" not in hits


async def test_filter_by_kind(pg_index: PostgresIndex) -> None:
    hits = await _ids(pg_index, "kind:shot", mode=SearchMode.LEXICAL)
    assert set(hits) <= {"shot:s1", "shot:s2"}


async def test_filter_by_facet(pg_index: PostgresIndex) -> None:
    hits = await _ids(pg_index, "render_mode:text_to_video", mode=SearchMode.LEXICAL)
    assert hits == ["shot:s2"]


async def test_range_filter(pg_index: PostgresIndex) -> None:
    hits = await _ids(pg_index, "duration_s:>=8", mode=SearchMode.LEXICAL)
    assert hits == ["shot:s2"]


async def test_book_scope(pg_index: PostgresIndex) -> None:
    hits = await _ids(pg_index, "meadow", mode=SearchMode.LEXICAL, book_id="b1")
    assert hits == []


async def test_semantic_arm(pg_index: PostgresIndex) -> None:
    embedder = FakeEmbedder()
    book = next(d for d in sample_docs() if d.doc_id == "book:b1")
    qvec = (await embedder.embed_texts([book.all_text()]))[0]
    req = SearchRequest(
        query=parse_query("fairy tale"),
        mode=SearchMode.SEMANTIC,
        query_embedding=list(qvec),
    )
    resp = await pg_index.search(req)
    assert resp.hits[0].doc_id == "book:b1"


async def test_hybrid_rrf(pg_index: PostgresIndex) -> None:
    embedder = FakeEmbedder()
    qvec = (await embedder.embed_texts(["frozen forest"]))[0]
    req = SearchRequest(
        query=parse_query("frozen"),
        mode=SearchMode.HYBRID,
        query_embedding=list(qvec),
    )
    resp = await pg_index.search(req)
    assert resp.total >= 1


async def test_facets(pg_index: PostgresIndex) -> None:
    req = SearchRequest(
        query=parse_query("frozen ice palace gerda meadow"),
        mode=SearchMode.LEXICAL,
        facet_fields=("kind",),
    )
    resp = await pg_index.search(req)
    counts = {fc.value: fc.count for f in resp.facets for fc in f.counts}
    assert counts


async def test_highlight(pg_index: PostgresIndex) -> None:
    req = SearchRequest(query=parse_query("snow queen"), mode=SearchMode.LEXICAL)
    resp = await pg_index.search(req)
    book_hit = next(h for h in resp.hits if h.doc_id == "book:b1")
    assert any("<mark>" in hl.snippet for hl in book_hit.highlights)


async def test_upsert_is_idempotent(pg_index: PostgresIndex) -> None:
    before = await pg_index.count()
    await pg_index.upsert([sample_docs()[0]])
    assert await pg_index.count() == before


async def test_delete_by_book(pg_index: PostgresIndex) -> None:
    n = await pg_index.delete_by_book("b1")
    assert n == 5
    assert await pg_index.count(book_id="b1") == 0


async def test_suggest(pg_index: PostgresIndex) -> None:
    out = await pg_index.suggest("fro")
    assert any(s.startswith("fro") for s in out)


async def test_versioned_alias_swap(session_factory: object) -> None:
    """A bulk reindex into a fresh version + alias swap leaves reads on the new one."""
    # Seed two versions: v1 (live) and a fresh one.
    embedder = FakeEmbedder()
    v1 = PostgresIndex(session_factory, index_version="v1")
    await v1.ensure_schema()
    docs = sample_docs()
    for d in docs:
        if d.embedding is None and d.all_text():
            d.embedding = (await embedder.embed_texts([d.all_text()]))[0]
    await v1.upsert(docs[:1])  # only the book in v1

    registry = InMemoryAliasRegistry({"kinora_current": "v1"})
    version2 = new_version()
    v2 = PostgresIndex(session_factory, index_version=version2)
    await v2.clear()
    await v2.upsert(docs)  # the full corpus in v2
    await registry.set_alias("kinora_current", version2)

    assert await registry.resolve("kinora_current") == version2
    # v2 has the full corpus; v1 still only the book (isolation between versions).
    assert await v2.count() == len(docs)
    assert await v1.count() == 1


async def test_pipeline_reindex_all_swaps_alias(session_factory: object) -> None:
    """The IndexingPipeline.reindex_all builds a fresh version and swaps the alias.

    Seeds a real book + page + beat row, then runs the pipeline end-to-end with a
    fake embedder (no network) and asserts the alias points at the new version
    holding the projected documents.
    """
    from app.db.models.book import Book, Page
    from app.db.models.enums import BookStatus

    async with session_factory() as session:  # type: ignore[operator]
        session.add(
            Book(id="bk", title="Reindex Tale", author="Tester", status=BookStatus.READY)
        )
        session.add(Page(id="pg", book_id="bk", page_number=1, text="A glittering frozen sea."))

    registry = InMemoryAliasRegistry({"kinora_current": "v1"})
    pipeline = IndexingPipeline(session_factory=session_factory, embedder=FakeEmbedder())

    def make_index(version: str) -> PostgresIndex:
        return PostgresIndex(session_factory, index_version=version)

    stats = await pipeline.reindex_all(
        make_index=make_index, alias_registry=registry, alias="kinora_current"
    )
    assert stats.total >= 2  # at least the book + page
    live_version = await registry.resolve("kinora_current")
    assert live_version == stats.index_version

    live = make_index(live_version or "v1")
    hits = await _ids(live, "frozen sea", mode=SearchMode.LEXICAL)
    assert "page:pg" in hits
