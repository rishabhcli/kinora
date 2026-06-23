"""EPUB normalisation — fold an ``.epub`` onto the §9.1 PDF extract path.

§9.1 step 1 is *"PDF → page images + text + layout (PyMuPDF)"*. EPUB support is
deliberately implemented as a **front-end to that exact step** rather than a
parallel pipeline: an EPUB is a reflowable XHTML document, and PyMuPDF (the
``fitz`` MuPDF binding already used for PDFs) opens ``filetype="epub"`` natively,
paginates the reflowed text, and converts it to a real PDF via
:meth:`fitz.Document.convert_to_pdf`. After that single conversion every
downstream stage — :func:`app.ingest.pdf_extract.extract_pdf` (page PNGs, text,
per-word boxes, the §4.2 source-span index), the VL analyse pass, canon build,
shot plan, scheduler, render — runs **byte-for-byte unchanged**, because it only
ever sees the normalised PDF. PDF and EPUB literally converge on one code path.

This module owns only the EPUB-specific bits that have no PDF analogue:

* :func:`looks_like_epub` — magic-byte detection (an EPUB is a ZIP whose first
  archived entry is the ``application/epub+zip`` ``mimetype`` stream);
* :func:`epub_to_pdf_bytes` — the MuPDF EPUB→PDF normalisation;
* :func:`epub_page_count` — a cheap reflow page count for the §11.1 page cap;
* :func:`extract_epub_metadata` — the OPF ``dc:title`` / ``dc:creator``;
* :func:`extract_epub_cover` — the declared cover image (EPUB3
  ``properties="cover-image"`` or the EPUB2 ``<meta name="cover">`` pointer),
  used to seed the book cover; absent it, the caller falls back to the rendered
  first page (which the PDF cover mechanism already serves as page 1).

Cover/metadata parsing uses only the stdlib (``zipfile`` + ``xml.etree``) — no
new third-party dependency is pulled in for EPUB support; PyMuPDF, already a
core dependency, does all the heavy lifting.
"""

from __future__ import annotations

import io
import posixpath
import zipfile
from xml.etree import ElementTree as ET

import fitz  # PyMuPDF

from app.core.logging import get_logger

logger = get_logger("app.ingest.epub_extract")

#: The canonical EPUB media type the apps send for an ``.epub`` upload.
EPUB_CONTENT_TYPE = "application/epub+zip"
#: The ``mimetype`` stream every conforming EPUB stores **first** and uncompressed.
_EPUB_MIMETYPE = b"application/epub+zip"
#: ZIP local-file-header magic ("PK\x03\x04") — an EPUB is a ZIP container.
_ZIP_MAGIC = b"PK\x03\x04"

# XML namespaces used by the OCF container + the OPF package document.
_CONTAINER_NS = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
_OPF = "{http://www.idpf.org/2007/opf}"
_DC = "{http://purl.org/dc/elements/1.1/}"

#: Image media types we are willing to serve as a cover, smallest sniff set.
_COVER_IMAGE_TYPES = frozenset(
    {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}
)


def looks_like_epub(data: bytes) -> bool:
    """Return whether ``data`` is an EPUB (ZIP container + the ``mimetype`` stream).

    Mirrors the PDF ``%PDF-`` magic check: cheap, content-based, and independent
    of the (spoofable) upload content-type. A conforming EPUB is a ZIP archive
    whose first entry is an uncompressed ``mimetype`` file holding exactly
    ``application/epub+zip``; we accept either that authoritative signal or, as a
    lenient fallback, any ZIP that carries a ``mimetype`` entry with the EPUB type.
    """
    if data[:4] != _ZIP_MAGIC:
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            if "mimetype" not in names:
                return False
            return zf.read("mimetype").strip() == _EPUB_MIMETYPE
    except (zipfile.BadZipFile, OSError, KeyError):
        return False


def _open_epub(data: bytes) -> fitz.Document:
    """Open EPUB ``data`` as a paginated MuPDF document (raises on a bad EPUB)."""
    return fitz.open(stream=data, filetype="epub")


def epub_page_count(data: bytes) -> int:
    """Cheap reflow page count for the §11.1 page cap (no rasterisation)."""
    doc = _open_epub(data)
    try:
        return int(doc.page_count)
    finally:
        doc.close()


def epub_to_pdf_bytes(data: bytes) -> bytes:
    """Normalise EPUB ``data`` to PDF bytes for the shared §9.1 extract path.

    MuPDF reflows the EPUB's XHTML into fixed pages with selectable text, then
    :meth:`convert_to_pdf` serialises that to a real PDF. The result feeds
    :func:`app.ingest.pdf_extract.extract_pdf` exactly like an uploaded PDF —
    same page PNGs, same per-word boxes, same global word index — so EPUB and PDF
    share one extraction/analysis/render pipeline downstream.
    """
    doc = _open_epub(data)
    try:
        pdf_bytes: bytes = doc.convert_to_pdf()
    finally:
        doc.close()
    logger.info("ingest.epub.converted", epub_bytes=len(data), pdf_bytes=len(pdf_bytes))
    return pdf_bytes


def extract_epub_metadata(data: bytes) -> tuple[str | None, str | None]:
    """Return ``(title, author)`` from the EPUB's metadata, best-effort.

    Prefers MuPDF's parsed metadata (``dc:title`` / ``dc:creator``); never raises
    — a malformed/absent field yields ``None`` and the caller falls back to the
    filename, exactly as the PDF path does.
    """
    try:
        doc = _open_epub(data)
    except Exception as exc:  # noqa: BLE001 - metadata is best-effort, never fatal
        logger.warning("ingest.epub.metadata_failed", error=str(exc))
        return (None, None)
    try:
        meta = doc.metadata or {}
    finally:
        doc.close()
    title = (meta.get("title") or "").strip() or None
    author = (meta.get("author") or "").strip() or None
    return (title, author)


def extract_epub_cover(data: bytes) -> tuple[bytes, str] | None:
    """Return ``(image_bytes, content_type)`` for the EPUB's declared cover, or ``None``.

    Resolves the cover via the OPF package document using only the stdlib:

    1. **EPUB 3** — the manifest ``<item>`` carrying ``properties="cover-image"``.
    2. **EPUB 2** — the ``<meta name="cover" content="<item-id>">`` pointer into
       the manifest.

    Returns ``None`` (no exception) when there is no declared cover or the EPUB is
    malformed; the caller then falls back to the rendered first page.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            opf_path = _rootfile_path(zf)
            if opf_path is None or opf_path not in names:
                return None
            opf = ET.fromstring(zf.read(opf_path))
            base = posixpath.dirname(opf_path)
            href, media_type = _cover_href(opf)
            if href is None:
                return None
            full = posixpath.normpath(posixpath.join(base, href)) if base else href
            if full not in names:
                return None
            image = zf.read(full)
            content_type = (media_type or "").strip().lower() or _guess_image_type(full)
            if content_type not in _COVER_IMAGE_TYPES:
                logger.warning("ingest.epub.cover_unsupported_type", content_type=content_type)
                return None
            return (image, content_type)
    except (zipfile.BadZipFile, OSError, KeyError, ET.ParseError) as exc:
        logger.warning("ingest.epub.cover_failed", error=str(exc))
        return None


def _rootfile_path(zf: zipfile.ZipFile) -> str | None:
    """Resolve the OPF package path from ``META-INF/container.xml``."""
    try:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
    except (KeyError, ET.ParseError):
        return None
    rootfile = container.find(".//c:rootfile", _CONTAINER_NS)
    if rootfile is None:
        return None
    full_path = rootfile.get("full-path")
    return full_path or None


def _cover_href(opf: ET.Element) -> tuple[str | None, str | None]:
    """Find the cover item's ``(href, media-type)`` in the OPF manifest."""
    manifest = opf.find(f"{_OPF}manifest")
    if manifest is None:
        return (None, None)
    items = manifest.findall(f"{_OPF}item")
    by_id = {it.get("id"): it for it in items if it.get("id")}

    # EPUB 3: the manifest item flagged properties="cover-image".
    for item in items:
        properties = (item.get("properties") or "").split()
        if "cover-image" in properties:
            return (item.get("href"), item.get("media-type"))

    # EPUB 2: <meta name="cover" content="<item-id>"> points into the manifest.
    metadata = opf.find(f"{_OPF}metadata")
    if metadata is not None:
        for meta in metadata.findall(f"{_OPF}meta"):
            if meta.get("name") == "cover":
                cover_item = by_id.get(meta.get("content") or "")
                if cover_item is not None:
                    return (cover_item.get("href"), cover_item.get("media-type"))

    return (None, None)


def sniff_image_content_type(data: bytes, default: str = "image/png") -> str:
    """Best-effort image content-type from magic bytes (cover served back from store).

    The page route serves the cover via a presigned URL; storing the right
    content-type keeps native image views happy. Falls back to ``default`` for an
    unrecognised header.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return default


def _guess_image_type(path: str) -> str:
    """Best-effort content-type from a cover file extension (manifest fallback)."""
    ext = posixpath.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    if ext == ".gif":
        return "image/gif"
    return ""


__all__ = [
    "EPUB_CONTENT_TYPE",
    "epub_page_count",
    "epub_to_pdf_bytes",
    "extract_epub_cover",
    "extract_epub_metadata",
    "looks_like_epub",
    "sniff_image_content_type",
]
