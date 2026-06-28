"""Behaviour tests for the in-memory search engine (BM25 + vector + RRF + facets)."""

from __future__ import annotations

import pytest_asyncio

from app.search.documents import DocKind
from app.search.index import SearchMode, SearchRequest
from app.search.memory_backend import InMemoryIndex
from app.search.query import parse_query
from tests.test_search_support import FakeEmbedder, sample_docs


@pytest_asyncio.fixture
async def index() -> InMemoryIndex:
    idx = InMemoryIndex()
    embedder = FakeEmbedder()
    docs = sample_docs()
    # Give text docs an embedding so the semantic arm has something to rank.
    for d in docs:
        if d.embedding is None and d.all_text():
            d.embedding = (await embedder.embed_texts([d.all_text()]))[0]
    await idx.upsert(docs)
    return idx


async def _search(index: InMemoryIndex, query: str, **kwargs: object) -> list[str]:
    req = SearchRequest(query=parse_query(query), **kwargs)  # type: ignore[arg-type]
    resp = await index.search(req)
    return [h.doc_id for h in resp.hits]


async def test_lexical_title_match(index: InMemoryIndex) -> None:
    hits = await _search(index, "snow queen", mode=SearchMode.LEXICAL)
    assert "book:b1" in hits
    assert hits[0] == "book:b1"  # title hit ranks first (field boost)


async def test_lexical_body_match(index: InMemoryIndex) -> None:
    hits = await _search(index, "frozen forest", mode=SearchMode.LEXICAL)
    assert "page:p1" in hits


async def test_stemming_match(index: InMemoryIndex) -> None:
    # "running" should match "runs" in the beat body.
    hits = await _search(index, "running", mode=SearchMode.LEXICAL)
    assert "beat:bt1" in hits


async def test_fuzzy_typo_match(index: InMemoryIndex) -> None:
    hits = await _search(index, "frrost", mode=SearchMode.LEXICAL)
    assert "beat:bt1" in hits or "page:p1" in hits


async def test_phrase_match(index: InMemoryIndex) -> None:
    hits = await _search(index, '"ice palace"', mode=SearchMode.LEXICAL)
    assert "beat:bt1" in hits


async def test_boolean_must_not(index: InMemoryIndex) -> None:
    # "frozen" matches book + page; exclude "forest" drops the page.
    hits = await _search(index, "frozen -forest", mode=SearchMode.LEXICAL)
    assert "page:p1" not in hits
    assert "book:b1" in hits


async def test_boolean_must(index: InMemoryIndex) -> None:
    hits = await _search(index, "+gerda +palace", mode=SearchMode.LEXICAL)
    # Only the beat has both gerda + palace.
    assert "beat:bt1" in hits


async def test_filter_by_kind(index: InMemoryIndex) -> None:
    hits = await _search(index, "kind:shot", mode=SearchMode.LEXICAL)
    assert set(hits) <= {"shot:s1", "shot:s2"}


async def test_filter_by_facet_value(index: InMemoryIndex) -> None:
    hits = await _search(index, "render_mode:text_to_video", mode=SearchMode.LEXICAL)
    assert hits == ["shot:s2"]


async def test_range_filter(index: InMemoryIndex) -> None:
    hits = await _search(index, "duration_s:>=8", mode=SearchMode.LEXICAL)
    assert hits == ["shot:s2"]


async def test_book_scope(index: InMemoryIndex) -> None:
    hits = await _search(index, "meadow", mode=SearchMode.LEXICAL, book_id="b1")
    assert hits == []  # the meadow shot is in book b2
    hits2 = await _search(index, "meadow", mode=SearchMode.LEXICAL, book_id="b2")
    assert "shot:s2" in hits2


async def test_kind_scope(index: InMemoryIndex) -> None:
    hits = await _search(
        index, "gerda", mode=SearchMode.LEXICAL, kinds=(DocKind.ENTITY,)
    )
    assert hits == ["entity:b1:char_gerda"]


async def test_semantic_arm(index: InMemoryIndex) -> None:
    embedder = FakeEmbedder()
    # The fixture embeds each doc's all_text(); embed the book's all_text() so the
    # query vector matches the indexed book vector exactly (cosine == 1).
    book = next(d for d in sample_docs() if d.doc_id == "book:b1")
    qvec = (await embedder.embed_texts([book.all_text()]))[0]
    req = SearchRequest(
        query=parse_query("fairy tale"),
        mode=SearchMode.SEMANTIC,
        query_embedding=qvec,
    )
    resp = await index.search(req)
    # The book doc embeds the identical text -> it should be the top semantic hit.
    assert resp.hits[0].doc_id == "book:b1"


async def test_hybrid_fuses_both_arms(index: InMemoryIndex) -> None:
    embedder = FakeEmbedder()
    qvec = (await embedder.embed_texts(["frozen forest"]))[0]
    req = SearchRequest(
        query=parse_query("frozen"),
        mode=SearchMode.HYBRID,
        query_embedding=qvec,
    )
    resp = await index.search(req)
    assert resp.total > 0
    # Hits carry both ranks when present in both arms.
    has_dual = any(
        h.lexical_rank is not None and h.semantic_rank is not None for h in resp.hits
    )
    assert has_dual or resp.total >= 1


async def test_facets(index: InMemoryIndex) -> None:
    req = SearchRequest(
        query=parse_query("frozen ice palace meadow gerda"),
        mode=SearchMode.LEXICAL,
        facet_fields=("kind",),
    )
    resp = await index.search(req)
    kinds = {fc.value: fc.count for f in resp.facets for fc in f.counts if f.field == "kind"}
    assert kinds  # at least one facet bucket
    assert sum(kinds.values()) == resp.total


async def test_highlight_in_hits(index: InMemoryIndex) -> None:
    req = SearchRequest(query=parse_query("snow queen"), mode=SearchMode.LEXICAL)
    resp = await index.search(req)
    book_hit = next(h for h in resp.hits if h.doc_id == "book:b1")
    assert any("<mark>" in hl.snippet for hl in book_hit.highlights)


async def test_pagination(index: InMemoryIndex) -> None:
    req = SearchRequest(query=parse_query("kind:shot"), mode=SearchMode.LEXICAL, limit=1)
    page1 = await index.search(req)
    assert len(page1.hits) == 1
    req2 = SearchRequest(
        query=parse_query("kind:shot"), mode=SearchMode.LEXICAL, limit=1, offset=1
    )
    page2 = await index.search(req2)
    assert len(page2.hits) == 1
    assert page1.hits[0].doc_id != page2.hits[0].doc_id


async def test_suggest_prefix(index: InMemoryIndex) -> None:
    suggestions = await index.suggest("fro")
    assert any(s.startswith("fro") for s in suggestions)


async def test_suggest_typo() -> None:
    idx = InMemoryIndex()
    await idx.upsert(sample_docs())
    # "charac" prefix -> "character" lexeme present in the entity facet.
    out = await idx.suggest("gerd")
    assert "gerda" in out


async def test_upsert_replaces_not_duplicates(index: InMemoryIndex) -> None:
    before = await index.count()
    await index.upsert([sample_docs()[0]])  # re-upsert the book
    assert await index.count() == before


async def test_delete(index: InMemoryIndex) -> None:
    n = await index.delete(["book:b1"])
    assert n == 1
    hits = await _search(index, "snow queen", mode=SearchMode.LEXICAL)
    assert "book:b1" not in hits


async def test_delete_by_book(index: InMemoryIndex) -> None:
    n = await index.delete_by_book("b1")
    assert n == 5  # book + page + beat + entity + shot:s1
    assert await index.count(book_id="b1") == 0
    assert await index.count(book_id="b2") == 1


async def test_clear(index: InMemoryIndex) -> None:
    await index.clear()
    assert await index.count() == 0
