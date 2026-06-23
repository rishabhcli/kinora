"""EPUB ingest — detection, EPUB→PDF normalisation, metadata, cover (§5.1/§9.1).

EPUB support folds onto the PDF extract path: an EPUB is normalised to PDF with
PyMuPDF and then runs through the *same* :func:`app.ingest.pdf_extract.extract_pdf`
as a PDF. These tests cover the EPUB-specific seams (the format detection, the
conversion, the cover/metadata pull) with no infra, plus a DB-gated convergence
test proving the converted EPUB yields the same page/text/word structure a PDF
does and that a declared cover becomes page 1's image.
"""

from __future__ import annotations

import io
import zipfile

import fitz
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.book import BookRepo, PageRepo
from app.ingest.epub_extract import (
    epub_page_count,
    epub_to_pdf_bytes,
    extract_epub_cover,
    extract_epub_metadata,
    looks_like_epub,
    sniff_image_content_type,
)
from app.ingest.pdf_extract import extract_pdf, page_image_key
from tests.test_ingest_support import (
    TINY_PNG,
    MemoryBlobStore,
    build_test_epub,
    build_test_pdf,
    requires_db,
    session,  # noqa: F401  (pytest fixture)
)

_CHAPTERS = [
    "Once upon a time a small red fox ran across the wide green field at dawn. "
    + ("The morning was bright and clear. " * 20),
    "The fox soon met a wise old owl who lived high in a tall ancient oak tree. "
    + ("They spoke of many distant lands. " * 20),
]


# --------------------------------------------------------------------------- #
# Detection (no infra)
# --------------------------------------------------------------------------- #


def test_looks_like_epub_accepts_real_epub() -> None:
    assert looks_like_epub(build_test_epub(_CHAPTERS)) is True


def test_looks_like_epub_rejects_pdf_and_junk() -> None:
    # A real PDF is not an EPUB.
    assert looks_like_epub(build_test_pdf(["hello"])) is False
    # Random bytes / a bare ZIP without the EPUB mimetype are not EPUBs.
    assert looks_like_epub(b"not an epub at all") is False
    assert looks_like_epub(b"") is False
    # A ZIP whose mimetype is wrong is rejected (magic, not extension).
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/zip")
    assert looks_like_epub(buf.getvalue()) is False


# --------------------------------------------------------------------------- #
# EPUB → PDF normalisation + page count (no infra)
# --------------------------------------------------------------------------- #


def test_epub_to_pdf_bytes_is_a_real_pdf_with_pages() -> None:
    epub = build_test_epub(_CHAPTERS)
    pdf = epub_to_pdf_bytes(epub)
    assert pdf[:5] == b"%PDF-"
    # The converted PDF carries the EPUB's reflowed text as real, selectable words.
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        assert doc.page_count >= 1
        words = doc.load_page(0).get_text("words")
        assert any(w[4] == "fox" for w in words)


def test_epub_page_count_matches_conversion() -> None:
    epub = build_test_epub(_CHAPTERS)
    count = epub_page_count(epub)
    assert count >= 1
    with fitz.open(stream=epub_to_pdf_bytes(epub), filetype="pdf") as doc:
        assert doc.page_count == count


# --------------------------------------------------------------------------- #
# Metadata + cover (no infra)
# --------------------------------------------------------------------------- #


def test_extract_epub_metadata_reads_title_and_author() -> None:
    epub = build_test_epub(_CHAPTERS, title="The Test Tale", author="Jane Writer")
    title, author = extract_epub_metadata(epub)
    assert title == "The Test Tale"
    assert author == "Jane Writer"


def test_extract_epub_cover_epub3_properties() -> None:
    epub = build_test_epub(_CHAPTERS, epub3_cover=True)
    cover = extract_epub_cover(epub)
    assert cover is not None
    data, content_type = cover
    assert data == TINY_PNG
    assert content_type == "image/png"


def test_extract_epub_cover_epub2_meta_pointer() -> None:
    epub = build_test_epub(_CHAPTERS, epub3_cover=False)
    cover = extract_epub_cover(epub)
    assert cover is not None
    assert cover[0] == TINY_PNG


def test_extract_epub_cover_absent_returns_none() -> None:
    epub = build_test_epub(_CHAPTERS, cover_png=None)
    assert extract_epub_cover(epub) is None


def test_extract_epub_cover_handles_malformed_epub() -> None:
    # A non-EPUB blob never raises — it yields no cover (the caller falls back).
    assert extract_epub_cover(b"definitely not a zip") is None


def test_sniff_image_content_type() -> None:
    assert sniff_image_content_type(TINY_PNG) == "image/png"
    assert sniff_image_content_type(b"\xff\xd8\xff\xe0junk") == "image/jpeg"
    assert sniff_image_content_type(b"RIFF\x00\x00\x00\x00WEBPxx") == "image/webp"
    assert sniff_image_content_type(b"GIF89a...") == "image/gif"
    # Unrecognised header falls back to the supplied default.
    assert sniff_image_content_type(b"????", default="image/png") == "image/png"


# --------------------------------------------------------------------------- #
# Convergence: a converted EPUB runs through the PDF extract path (DB-gated)
# --------------------------------------------------------------------------- #


@requires_db
async def test_converted_epub_extracts_like_a_pdf(
    session: AsyncSession,  # noqa: F811
) -> None:
    """The EPUB→PDF normalisation yields the same page/text/word structure as a PDF."""
    store = MemoryBlobStore()
    book = await BookRepo(session).create(title="The Fox and the Owl (EPUB)")
    pdf = epub_to_pdf_bytes(build_test_epub(_CHAPTERS))

    result = await extract_pdf(
        PageRepo(session), book_id=book.id, pdf_bytes=pdf, blob_store=store
    )

    # Pages + a real global word index, exactly as the PDF path produces.
    assert result.num_pages >= 1
    assert result.total_words > 0
    rows = await PageRepo(session).list_for_book(book.id)
    assert rows and all(r.text for r in rows)
    assert any("fox" in (r.text or "") for r in rows)

    # Per-word boxes are normalised into [0, 1] page coordinates (karaoke geometry).
    indices = [wb["word_index"] for r in rows for wb in (r.word_boxes or [])]
    assert indices == list(range(len(indices)))
    for row in rows:
        for box in row.word_boxes or []:
            x, y, w, h = box["bbox"]
            assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
            assert x + w <= 1.0001 and y + h <= 1.0001

    # Every page PNG was uploaded under the shared page-image key.
    for page in range(1, result.num_pages + 1):
        assert store.exists(page_image_key(book.id, page))


@requires_db
async def test_epub_cover_becomes_page_one_image(
    session: AsyncSession,  # noqa: F811
) -> None:
    """A declared EPUB cover is stored as page 1's image (the cover mechanism)."""
    store = MemoryBlobStore()
    book = await BookRepo(session).create(title="Covered Tale")
    epub = build_test_epub(_CHAPTERS)
    cover = extract_epub_cover(epub)
    assert cover is not None

    await extract_pdf(
        PageRepo(session),
        book_id=book.id,
        pdf_bytes=epub_to_pdf_bytes(epub),
        blob_store=store,
        cover_image=cover,
    )

    # Page 1's stored image is the EPUB's cover (not the rendered first page PNG).
    page1_key = page_image_key(book.id, 1)
    assert store.get_bytes(page1_key) == TINY_PNG
    # A page beyond the first remains a rendered PNG when there is more than one.
    rows = await PageRepo(session).list_for_book(book.id)
    if len(rows) > 1:
        assert store.get_bytes(page_image_key(book.id, 2))[:8] == b"\x89PNG\r\n\x1a\n"


@requires_db
async def test_extract_without_cover_renders_page_one(
    session: AsyncSession,  # noqa: F811
) -> None:
    """Without a cover override page 1 is the rendered page (PDF behaviour intact)."""
    store = MemoryBlobStore()
    book = await BookRepo(session).create(title="No Cover")
    await extract_pdf(
        PageRepo(session),
        book_id=book.id,
        pdf_bytes=build_test_pdf(["A plain page of text with several words on it."]),
        blob_store=store,
        cover_image=None,
    )
    assert store.get_bytes(page_image_key(book.id, 1))[:8] == b"\x89PNG\r\n\x1a\n"
