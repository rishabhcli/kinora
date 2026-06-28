"""PDF extraction — page images + text + per-word boxes + the global word index.

This is §9.1 step 1 (the "Extract" step) and the backbone of the §4.2 source-span
index. Using PyMuPDF (``fitz``) it, per page:

* renders the page to a PNG at a reasonable DPI and uploads it to object storage;
* extracts the page text **and** every word's bounding box via
  ``page.get_text("words", sort=True)``, then **re-threads reading order** with
  :mod:`app.ingest.layout` so multi-column pages read column-by-column instead of
  zig-zagging across the gutter (correct reading order is what makes the
  book-global word index match how a human reads);
* assigns each word a **book-global, monotonically increasing** ``word_index``
  (page *N+1* starts where page *N* ended) — this integer is the key the
  source-span index sorts on to turn a scroll position into a shot in O(log n);
* normalises every bbox to ``[0, 1]`` page coordinates so the karaoke highlight
  layer can paint it regardless of the rendered page size;
* falls back to **OCR** (:mod:`app.ingest.ocr`) for a page whose text layer is
  empty/sparse but whose rendered image clearly holds text (a scanned page), so
  image-only books still contribute words to the index instead of leaving gaps.

**Bounded memory at any book size.** Pages are processed **one at a time** — each
page is rendered, uploaded, and persisted before the next is loaded, and the
returned :class:`PdfExtractResult` holds only lightweight per-page metadata (no
PNG bytes), so a 2000-page / 1 GB book ingests without buffering every rendered
page in RAM.

The extracted rows are persisted via :class:`app.db.repositories.book.PageRepo`
and a structured :class:`PdfExtractResult` (carrying the per-page global word
ranges) is returned for the downstream analyse / shot-plan steps.

Note: page PNG keys use :meth:`app.storage.object_store.Keys.page_image`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import anyio
import fitz  # PyMuPDF
from pydantic import BaseModel, ConfigDict, Field

from app.core.logging import get_logger
from app.db.repositories.book import PageRepo
from app.ingest.layout import order_raw_words
from app.ingest.ocr import OcrEngine, looks_scanned, ocr_page
from app.memory.interfaces import BlobStore
from app.storage.object_store import keys

logger = get_logger("app.ingest.pdf_extract")

#: Render resolution for page PNGs. 150 DPI (≈2.08× the 72-dpi PDF point grid) is
#: crisp enough for Qwen-VL page reading without bloating object storage.
DEFAULT_DPI = 150
_PDF_POINTS_PER_INCH = 72.0
_PNG_CONTENT_TYPE = "image/png"
#: Default word floor under which a page is OCR-candidate (mirrors the setting).
DEFAULT_OCR_WORD_FLOOR = 12

#: ``async (current_page, total_pages)`` per-page progress sink (optional).
PageProgress = Callable[[int, int], Awaitable[None]]


def page_image_key(book_id: str, page_number: int) -> str:
    """Object key for a rendered page PNG (delegates to :meth:`Keys.page_image`)."""
    return keys.page_image(book_id, page_number)


class WordBox(BaseModel):
    """One extracted word: its book-global index, text, and normalised bbox.

    ``bbox`` is ``[x, y, w, h]`` in ``[0, 1]`` page coordinates (the §9.4 word
    geometry the highlight layer paints).
    """

    model_config = ConfigDict(extra="forbid")

    word_index: int
    text: str
    bbox: tuple[float, float, float, float]

    def as_row(self) -> dict[str, object]:
        """Serialise to the JSONB shape stored in ``pages.word_boxes``."""
        return {"word_index": self.word_index, "text": self.text, "bbox": list(self.bbox)}


class PageExtract(BaseModel):
    """The structured result of extracting one page."""

    model_config = ConfigDict(extra="forbid")

    page_number: int
    image_key: str
    text: str
    word_boxes: list[WordBox] = Field(default_factory=list)
    #: Book-global index of this page's first word (inclusive).
    word_index_start: int
    #: Book-global index one past this page's last word (exclusive); equals
    #: ``word_index_start`` for a page with no extractable words (e.g. a full-page
    #: illustration that OCR also could not read).
    word_index_end: int
    #: Whether this page's words came from OCR (the text layer was empty/sparse).
    from_ocr: bool = False

    @property
    def num_words(self) -> int:
        """Number of extractable words on the page."""
        return len(self.word_boxes)


class PdfExtractResult(BaseModel):
    """The structured per-book extraction result handed to later steps.

    Holds only lightweight per-page metadata — never the rendered PNG bytes — so
    it stays small even for a very large book.
    """

    model_config = ConfigDict(extra="forbid")

    book_id: str
    num_pages: int
    pages: list[PageExtract] = Field(default_factory=list)
    total_words: int = 0
    #: How many pages fell back to OCR (telemetry / quality signal).
    num_ocr_pages: int = 0


def _normalise_bbox(
    x0: float, y0: float, x1: float, y1: float, width: float, height: float
) -> tuple[float, float, float, float]:
    """Convert an absolute PDF rect to a clamped ``[x, y, w, h]`` in ``[0, 1]``."""
    if width <= 0 or height <= 0:
        return (0.0, 0.0, 0.0, 0.0)

    def clamp(value: float) -> float:
        return max(0.0, min(1.0, value))

    nx = clamp(x0 / width)
    ny = clamp(y0 / height)
    nw = clamp((x1 - x0) / width)
    nh = clamp((y1 - y0) / height)
    # Keep the box inside the page even when the raw rect slightly overflows.
    nw = min(nw, 1.0 - nx)
    nh = min(nh, 1.0 - ny)
    return (round(nx, 5), round(ny, 5), round(nw, 5), round(nh, 5))


def _render_png(page: fitz.Page, dpi: int) -> bytes:
    """Rasterise a page to PNG bytes at ``dpi``."""
    zoom = dpi / _PDF_POINTS_PER_INCH
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    data: bytes = pix.tobytes("png")
    return data


def _extract_page_words(
    page: fitz.Page, *, page_number: int, book_id: str, dpi: int, word_offset: int
) -> tuple[PageExtract, bytes]:
    """Extract one page's text + word boxes (reading-ordered) and render its PNG.

    Pure of any I/O sink: the caller decides where the PNG goes. Words are
    re-threaded into human reading order (:mod:`app.ingest.layout`) before the
    book-global ``word_index`` is assigned, so multi-column pages index correctly.
    """
    rect = page.rect
    width, height = float(rect.width), float(rect.height)

    raw_words = page.get_text("words", sort=True)
    ordered = order_raw_words(raw_words, width, height)
    boxes: list[WordBox] = [
        WordBox(
            word_index=word_offset + i,
            text=word.text,
            bbox=_normalise_bbox(word.x0, word.y0, word.x1, word.y1, width, height),
        )
        for i, word in enumerate(ordered)
    ]

    # Reconstruct page text from the reading-ordered words so the text the Adapter
    # reads is word-for-word aligned with the boxes the source-span index
    # reconciles against; fall back to native text only if no words extracted.
    text = " ".join(box.text for box in boxes) or page.get_text("text").strip()
    png = _render_png(page, dpi)
    extract = PageExtract(
        page_number=page_number,
        image_key=page_image_key(book_id, page_number),
        text=text,
        word_boxes=boxes,
        word_index_start=word_offset,
        word_index_end=word_offset + len(boxes),
    )
    return extract, png


def _apply_ocr(
    base: PageExtract,
    ocr_text: str,
    ocr_words: list[tuple[str, tuple[float, float, float, float]]],
) -> PageExtract:
    """Rebuild a page extract from OCR output, re-indexing word boxes globally."""
    boxes = [
        WordBox(word_index=base.word_index_start + i, text=text, bbox=bbox)
        for i, (text, bbox) in enumerate(ocr_words)
    ]
    return PageExtract(
        page_number=base.page_number,
        image_key=base.image_key,
        text=ocr_text or base.text,
        word_boxes=boxes,
        word_index_start=base.word_index_start,
        word_index_end=base.word_index_start + len(boxes),
        from_ocr=True,
    )


async def extract_pdf(
    session_pages: PageRepo,
    *,
    book_id: str,
    pdf_bytes: bytes,
    blob_store: BlobStore,
    dpi: int = DEFAULT_DPI,
    cover_image: tuple[bytes, str] | None = None,
    ocr_engine: OcrEngine | None = None,
    ocr_word_floor: int = DEFAULT_OCR_WORD_FLOOR,
    page_progress: PageProgress | None = None,
) -> PdfExtractResult:
    """Extract, render, upload, and persist every page of a PDF (streaming).

    Idempotent on re-run: pages already present for ``book_id`` (matched by
    ``page_number``) are not re-inserted, so a crashed/partial ingest can be
    safely retried. Pages are processed one at a time so a very large book does
    not buffer every rendered PNG in memory.

    Args:
        session_pages: a :class:`PageRepo` bound to the active unit-of-work.
        book_id: the book these pages belong to.
        pdf_bytes: the raw uploaded PDF (for non-PDF uploads, the normalised PDF
            from :mod:`app.ingest.formats` / :mod:`app.ingest.epub_extract`).
        blob_store: object store the page PNGs are uploaded to.
        dpi: render resolution for the page PNGs.
        cover_image: optional ``(bytes, content_type)`` for a publisher-supplied
            cover (an EPUB's declared cover image). When present it is stored as
            **page 1's image** in place of the rendered first page.
        ocr_engine: optional OCR backend (:mod:`app.ingest.ocr`). When supplied, a
            page whose text layer is empty/sparse but whose rendered image is
            substantial is transcribed and its synthesised word boxes are used.
        ocr_word_floor: page word-count below which OCR is considered.
        page_progress: optional ``async (current_page, total_pages)`` callback.

    Returns:
        The structured per-page result (incl. the global word ranges).
    """
    existing = {p.page_number for p in await session_pages.list_for_book(book_id)}

    pages: list[PageExtract] = []
    word_offset = 0
    num_ocr_pages = 0
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        num_pages = doc.page_count
        for index in range(num_pages):
            page = doc.load_page(index)
            page_number = index + 1
            extract, png = _extract_page_words(
                page,
                page_number=page_number,
                book_id=book_id,
                dpi=dpi,
                word_offset=word_offset,
            )

            # Page 1's image is the supplied cover when one was provided (EPUB);
            # otherwise the rendered page. Upload off the event loop (boto3 blocks).
            if page_number == 1 and cover_image is not None:
                cover_bytes, cover_type = cover_image
                await anyio.to_thread.run_sync(
                    blob_store.put_bytes, extract.image_key, cover_bytes, cover_type
                )
            else:
                await anyio.to_thread.run_sync(
                    blob_store.put_bytes, extract.image_key, png, _PNG_CONTENT_TYPE
                )

            # OCR fallback: a sparse text layer over a substantial page image is a
            # scan — transcribe it so the page contributes words to the index.
            if (
                ocr_engine is not None
                and extract.num_words < ocr_word_floor
                and looks_scanned(num_text_words=extract.num_words, image_size_bytes=len(png))
            ):
                ocr = await ocr_page(ocr_engine, png, page_number=page_number)
                if ocr.num_words:
                    extract = _apply_ocr(
                        extract, ocr.text, [(w.text, w.bbox) for w in ocr.words]
                    )
                    num_ocr_pages += 1

            if page_number not in existing:
                await session_pages.create(
                    book_id=book_id,
                    page_number=page_number,
                    image_key=extract.image_key,
                    text=extract.text,
                    word_boxes=[box.as_row() for box in extract.word_boxes],
                )
            word_offset = extract.word_index_end
            pages.append(extract)
            if page_progress is not None:
                await page_progress(page_number, num_pages)

    result = PdfExtractResult(
        book_id=book_id,
        num_pages=len(pages),
        pages=pages,
        total_words=word_offset,
        num_ocr_pages=num_ocr_pages,
    )
    logger.info(
        "ingest.extract.done",
        book_id=book_id,
        num_pages=result.num_pages,
        total_words=result.total_words,
        ocr_pages=num_ocr_pages,
    )
    return result


__all__ = [
    "DEFAULT_DPI",
    "DEFAULT_OCR_WORD_FLOOR",
    "PageExtract",
    "PageProgress",
    "PdfExtractResult",
    "WordBox",
    "extract_pdf",
    "page_image_key",
]
