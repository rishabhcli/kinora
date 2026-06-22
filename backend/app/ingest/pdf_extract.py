"""PDF extraction — page images + text + per-word boxes + the global word index.

This is §9.1 step 1 (the "Extract" step) and the backbone of the §4.2 source-span
index. Using PyMuPDF (``fitz``) it, per page:

* renders the page to a PNG at a reasonable DPI and uploads it to object storage;
* extracts the page text **and** every word's bounding box via
  ``page.get_text("words", sort=True)`` (reading order);
* assigns each word a **book-global, monotonically increasing** ``word_index``
  (page *N+1* starts where page *N* ended) — this integer is the key the
  source-span index sorts on to turn a scroll position into a shot in O(log n);
* normalises every bbox to ``[0, 1]`` page coordinates so the karaoke highlight
  layer can paint it regardless of the rendered page size.

The extracted rows are persisted via :class:`app.db.repositories.book.PageRepo`
and a structured :class:`PdfExtractResult` (carrying the per-page global word
ranges) is returned for the downstream analyse / shot-plan steps.

Note: the off-limits ``app.storage`` layer ships key builders for clips /
keyframes / refs / audio / pdfs but not for page images, so the page-image key
helper :func:`page_image_key` lives here (a plain ``put_bytes`` to a stable
``pages/<book>/<n>.png`` key — no change to the storage client is required).
"""

from __future__ import annotations

import anyio
import fitz  # PyMuPDF
from pydantic import BaseModel, ConfigDict, Field

from app.core.logging import get_logger
from app.db.repositories.book import PageRepo
from app.memory.interfaces import BlobStore

logger = get_logger("app.ingest.pdf_extract")

#: Render resolution for page PNGs. 150 DPI (≈2.08× the 72-dpi PDF point grid) is
#: crisp enough for Qwen-VL page reading without bloating object storage.
DEFAULT_DPI = 150
_PDF_POINTS_PER_INCH = 72.0
_PNG_CONTENT_TYPE = "image/png"


def page_image_key(book_id: str, page_number: int) -> str:
    """Object key for a rendered page PNG (``pages/<book>/<n>.png``)."""
    return f"pages/{book_id}/{page_number:04d}.png"


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
    #: illustration).
    word_index_end: int

    @property
    def num_words(self) -> int:
        """Number of extractable words on the page."""
        return len(self.word_boxes)


class PdfExtractResult(BaseModel):
    """The structured per-book extraction result handed to later steps."""

    model_config = ConfigDict(extra="forbid")

    book_id: str
    num_pages: int
    pages: list[PageExtract] = Field(default_factory=list)
    total_words: int = 0


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


def _extract_page(
    page: fitz.Page, *, page_number: int, book_id: str, dpi: int, word_offset: int
) -> tuple[PageExtract, bytes]:
    """Extract one page's text + word boxes and render its PNG (pure, no I/O sink)."""
    rect = page.rect
    width, height = float(rect.width), float(rect.height)

    # (x0, y0, x1, y1, "word", block_no, line_no, word_no), sorted into reading order.
    raw_words = page.get_text("words", sort=True)
    boxes: list[WordBox] = []
    for word in raw_words:
        x0, y0, x1, y1, text = word[0], word[1], word[2], word[3], word[4]
        if not str(text).strip():
            continue
        boxes.append(
            WordBox(
                word_index=word_offset + len(boxes),
                text=str(text),
                bbox=_normalise_bbox(
                    float(x0), float(y0), float(x1), float(y1), width, height
                ),
            )
        )

    # Reconstruct the page text from the (reading-order) words so the text the
    # Adapter reads is word-for-word aligned with the boxes the source-span index
    # reconciles against; fall back to the native text only if no words extracted.
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


async def extract_pdf(
    session_pages: PageRepo,
    *,
    book_id: str,
    pdf_bytes: bytes,
    blob_store: BlobStore,
    dpi: int = DEFAULT_DPI,
) -> PdfExtractResult:
    """Extract, render, upload, and persist every page of a PDF.

    Idempotent on re-run: pages already present for ``book_id`` (matched by
    ``page_number``) are not re-inserted, so a crashed/partial ingest can be
    safely retried.

    Args:
        session_pages: a :class:`PageRepo` bound to the active unit-of-work.
        book_id: the book these pages belong to.
        pdf_bytes: the raw uploaded PDF.
        blob_store: object store the page PNGs are uploaded to.
        dpi: render resolution for the page PNGs.

    Returns:
        The structured per-page result (incl. the global word ranges).
    """
    existing = {p.page_number for p in await session_pages.list_for_book(book_id)}

    pages: list[PageExtract] = []
    word_offset = 0
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        num_pages = doc.page_count
        for index in range(num_pages):
            page = doc.load_page(index)
            page_number = index + 1
            extract, png = _extract_page(
                page,
                page_number=page_number,
                book_id=book_id,
                dpi=dpi,
                word_offset=word_offset,
            )
            # Upload the rendered page off the event loop (boto3 is blocking).
            await anyio.to_thread.run_sync(
                blob_store.put_bytes, extract.image_key, png, _PNG_CONTENT_TYPE
            )
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

    result = PdfExtractResult(
        book_id=book_id,
        num_pages=len(pages),
        pages=pages,
        total_words=word_offset,
    )
    logger.info(
        "ingest.extract.done",
        book_id=book_id,
        num_pages=result.num_pages,
        total_words=result.total_words,
    )
    return result


__all__ = [
    "DEFAULT_DPI",
    "PageExtract",
    "PdfExtractResult",
    "WordBox",
    "extract_pdf",
    "page_image_key",
]
