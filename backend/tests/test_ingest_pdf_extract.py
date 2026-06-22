"""PyMuPDF extraction against a real PDF + throwaway Postgres (§9.1 step 1)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.book import BookRepo, PageRepo
from app.ingest.pdf_extract import extract_pdf, page_image_key
from tests.test_ingest_support import (
    MemoryBlobStore,
    build_test_pdf,
    requires_db,
    session,  # noqa: F401  (pytest fixture)
)

pytestmark = requires_db

_PAGES = [
    "Once upon a time a small red fox ran across the wide green field at dawn.",
    "The fox soon met a wise old owl who lived high in a tall ancient oak tree.",
    "Together they wandered down to the cold river where a silver fish leaped.",
]


async def test_extract_persists_pages_words_and_uploads_images(
    session: AsyncSession,  # noqa: F811
) -> None:
    store = MemoryBlobStore()
    book = await BookRepo(session).create(title="The Fox and the Owl")

    result = await extract_pdf(
        PageRepo(session), book_id=book.id, pdf_bytes=build_test_pdf(_PAGES), blob_store=store
    )

    # Structured result mirrors the PDF.
    assert result.num_pages == 3
    assert result.total_words > 0
    assert [p.page_number for p in result.pages] == [1, 2, 3]

    # Pages persisted with text + word boxes.
    rows = await PageRepo(session).list_for_book(book.id)
    assert len(rows) == 3
    assert all(r.text for r in rows)
    assert all(r.word_boxes for r in rows)

    # The book-global word_index is strictly increasing and contiguous across pages.
    indices = [wb["word_index"] for r in rows for wb in (r.word_boxes or [])]
    assert indices == list(range(len(indices)))
    assert indices[0] == 0

    # Per-page global ranges chain: page N+1 starts where page N ended.
    assert result.pages[0].word_index_start == 0
    for prev, nxt in zip(result.pages[:-1], result.pages[1:], strict=True):
        assert nxt.word_index_start == prev.word_index_end
    assert result.pages[-1].word_index_end == result.total_words == len(indices)

    # Every bbox is normalised to [0, 1] and stays inside the page.
    for row in rows:
        for box in row.word_boxes or []:
            x, y, w, h = box["bbox"]
            assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
            assert 0.0 <= w <= 1.0 and 0.0 <= h <= 1.0
            assert x + w <= 1.0001 and y + h <= 1.0001

    # The page PNGs were uploaded to object storage under the page-image key.
    for page in range(1, 4):
        key = page_image_key(book.id, page)
        assert store.exists(key)
        assert store.get_bytes(key)[:8] == b"\x89PNG\r\n\x1a\n"  # real PNG bytes


async def test_extract_is_idempotent_on_rerun(session: AsyncSession) -> None:  # noqa: F811
    store = MemoryBlobStore()
    book = await BookRepo(session).create(title="Rerun")
    pdf = build_test_pdf(_PAGES[:2])

    first = await extract_pdf(PageRepo(session), book_id=book.id, pdf_bytes=pdf, blob_store=store)
    # A second run must not violate the (book_id, page_number) unique constraint.
    second = await extract_pdf(PageRepo(session), book_id=book.id, pdf_bytes=pdf, blob_store=store)

    assert first.num_pages == second.num_pages == 2
    rows = await PageRepo(session).list_for_book(book.id)
    assert len(rows) == 2  # no duplicate page rows
