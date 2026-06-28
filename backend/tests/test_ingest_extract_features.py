"""Extraction enhancements: OCR fallback, layout reorder, page-progress (§9.1).

DB-backed (throwaway pgvector Postgres; SKIPs when ``KINORA_TEST_DATABASE_URL``
is unset). The OCR engine is a fake — no network. These cover the new streaming
``extract_pdf`` hooks added by the ingest overhaul.
"""

from __future__ import annotations

import fitz  # PyMuPDF
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


def _image_only_pdf(num_pages: int = 1) -> bytes:
    """A PDF whose pages carry only a rendered image (no text layer) — a 'scan'."""
    doc = fitz.open()
    try:
        for _ in range(num_pages):
            page = doc.new_page(width=612, height=792)
            # Paint a filled rectangle so the rendered PNG is substantial (>8 KiB),
            # but insert NO text — get_text("words") yields nothing.
            page.draw_rect(fitz.Rect(20, 20, 592, 772), color=(0, 0, 0), fill=(0.2, 0.3, 0.4))
        return doc.tobytes()
    finally:
        doc.close()


class _FakeOcr:
    """A fake OCR engine returning fixed text per page (no network)."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pages: list[int] = []

    async def transcribe(self, image: bytes, *, page_number: int) -> str:
        self.pages.append(page_number)
        return self.text


async def test_ocr_fallback_fills_words_for_scanned_page(
    session: AsyncSession,  # noqa: F811
) -> None:
    store = MemoryBlobStore()
    book = await BookRepo(session).create(title="Scanned")
    engine = _FakeOcr("The recovered scanned line one. The recovered scanned line two.")

    result = await extract_pdf(
        PageRepo(session),
        book_id=book.id,
        pdf_bytes=_image_only_pdf(1),
        blob_store=store,
        ocr_engine=engine,
    )

    assert result.num_ocr_pages == 1
    assert result.total_words > 0
    page = result.pages[0]
    assert page.from_ocr is True
    assert page.num_words > 0
    # Persisted with the OCR'd words + synthesised boxes.
    rows = await PageRepo(session).list_for_book(book.id)
    assert rows[0].word_boxes
    assert engine.pages == [1]


async def test_no_ocr_when_text_layer_is_good(session: AsyncSession) -> None:  # noqa: F811
    store = MemoryBlobStore()
    book = await BookRepo(session).create(title="Born digital")
    engine = _FakeOcr("SHOULD NOT BE USED")

    long_page = " ".join(f"word{i}" for i in range(60))
    result = await extract_pdf(
        PageRepo(session),
        book_id=book.id,
        pdf_bytes=build_test_pdf([long_page]),
        blob_store=store,
        ocr_engine=engine,
    )

    assert result.num_ocr_pages == 0
    assert result.pages[0].from_ocr is False
    assert engine.pages == []  # the good text layer means OCR is never invoked


async def test_two_column_pages_read_column_by_column(
    session: AsyncSession,  # noqa: F811
) -> None:
    store = MemoryBlobStore()
    book = await BookRepo(session).create(title="Two column")

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_textbox(
        fitz.Rect(40, 40, 280, 752),
        "Leftone lefttwo leftthree leftfour leftfive leftsix leftseven lefteight.",
        fontsize=11,
    )
    page.insert_textbox(
        fitz.Rect(330, 40, 572, 752),
        "Rightone righttwo rightthree rightfour rightfive rightsix rightseven.",
        fontsize=11,
    )
    pdf = doc.tobytes()
    doc.close()

    result = await extract_pdf(
        PageRepo(session), book_id=book.id, pdf_bytes=pdf, blob_store=store
    )
    texts = [w.text for w in result.pages[0].word_boxes]
    left_idx = [i for i, t in enumerate(texts) if t.startswith("left")]
    right_idx = [i for i, t in enumerate(texts) if t.startswith("right")]
    assert left_idx and right_idx
    # All left-column words index before all right-column words.
    assert max(left_idx) < min(right_idx)


async def test_page_progress_callback_fires_per_page(
    session: AsyncSession,  # noqa: F811
) -> None:
    store = MemoryBlobStore()
    book = await BookRepo(session).create(title="Progress")
    seen: list[tuple[int, int]] = []

    async def progress(current: int, total: int) -> None:
        seen.append((current, total))

    pages = ["First page words here.", "Second page words here.", "Third page text."]
    await extract_pdf(
        PageRepo(session),
        book_id=book.id,
        pdf_bytes=build_test_pdf(pages),
        blob_store=store,
        page_progress=progress,
    )
    assert seen == [(1, 3), (2, 3), (3, 3)]


async def test_global_word_index_contiguous_with_ocr_pages(
    session: AsyncSession,  # noqa: F811
) -> None:
    """A mix of text pages and OCR'd pages still produces a gapless global index."""
    store = MemoryBlobStore()
    book = await BookRepo(session).create(title="Mixed")
    engine = _FakeOcr("ocr alpha beta gamma delta epsilon zeta eta theta iota kappa.")

    # Build: one good text page, then one image-only page.
    good = build_test_pdf([" ".join(f"text{i}" for i in range(40))])
    scanned = _image_only_pdf(1)
    # Concatenate the two PDFs into one document.
    merged = fitz.open()
    for src_bytes in (good, scanned):
        with fitz.open(stream=src_bytes, filetype="pdf") as src:
            merged.insert_pdf(src)
    pdf = merged.tobytes()
    merged.close()

    result = await extract_pdf(
        PageRepo(session),
        book_id=book.id,
        pdf_bytes=pdf,
        blob_store=store,
        ocr_engine=engine,
    )
    assert result.num_pages == 2
    # Page ranges chain with no gap.
    assert result.pages[0].word_index_start == 0
    assert result.pages[1].word_index_start == result.pages[0].word_index_end
    assert result.pages[-1].word_index_end == result.total_words


async def test_result_holds_no_png_bytes(session: AsyncSession) -> None:  # noqa: F811
    """Bounded memory: the extract result must not retain rendered PNG bytes."""
    store = MemoryBlobStore()
    book = await BookRepo(session).create(title="Bounded")
    result = await extract_pdf(
        PageRepo(session),
        book_id=book.id,
        pdf_bytes=build_test_pdf(["page one words", "page two words"]),
        blob_store=store,
    )
    # The structured result is small: per-page fields are metadata + word boxes,
    # never the PNG. The PNGs live only in object storage.
    dumped = result.model_dump()
    blob = repr(dumped).encode("utf-8", errors="ignore")
    assert b"\x89PNG" not in blob
    # The PNGs *are* in the store, keyed per page.
    assert store.exists(page_image_key(book.id, 1))


async def test_streaming_large_book_completes(session: AsyncSession) -> None:  # noqa: F811
    """A many-page book extracts to a gapless, monotonic global index (streaming)."""
    store = MemoryBlobStore()
    book = await BookRepo(session).create(title="Long")
    pages = [f"Page {i} has a handful of words to index here today." for i in range(60)]
    result = await extract_pdf(
        PageRepo(session), book_id=book.id, pdf_bytes=build_test_pdf(pages), blob_store=store
    )
    assert result.num_pages == 60
    # Indices are strictly chained across every page with no gaps.
    cursor = 0
    for page in result.pages:
        assert page.word_index_start == cursor
        cursor = page.word_index_end
    assert result.total_words == cursor
