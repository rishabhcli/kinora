"""Input-format funnel — every supported source normalises to PDF bytes (§9.1).

§9.1 step 1 is *"PDF → page images + text + layout (PyMuPDF)"*. Like the EPUB
front-end (:mod:`app.ingest.epub_extract`), every other input format is folded
onto that **one** extract path by converting it to a real PDF first, so the
whole downstream pipeline (extract → analyse → canon → shot-plan → render) runs
byte-for-byte unchanged regardless of what the user uploaded.

Supported inputs and how they reach PDF:

============  ==================================================================
Format         Normalisation
============  ==================================================================
PDF            verbatim (already a PDF)
EPUB           PyMuPDF ``convert_to_pdf`` (:mod:`app.ingest.epub_extract`)
TXT            wrapped in minimal HTML, laid out by PyMuPDF ``Story`` → PDF
Markdown       parsed by a tiny stdlib Markdown→HTML, laid out via ``Story`` → PDF
HTML           sanitised body, laid out via ``Story`` → PDF
DOCX           stdlib ``zipfile`` + ``xml.etree`` over ``word/document.xml`` →
               paragraphs/headings → HTML → ``Story`` → PDF
MOBI / AZW3    detected + rejected with a clear "convert to EPUB" message (the
               legacy Mobipocket/KF8 container needs a heavyweight decoder we
               deliberately do not bundle; EPUB is the lossless substitute)
============  ==================================================================

Everything here uses **only** PyMuPDF (already a core dependency) and the
stdlib — no new third-party package is pulled in, exactly as the EPUB path
established. The conversions are pure functions over ``bytes`` so they are fully
unit-testable with no DB / object-store / network.
"""

from __future__ import annotations

import enum
import html as _html
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import fitz  # PyMuPDF

from app.core.logging import get_logger
from app.ingest.epub_extract import epub_to_pdf_bytes, looks_like_epub

logger = get_logger("app.ingest.formats")

# --------------------------------------------------------------------------- #
# Format identity
# --------------------------------------------------------------------------- #

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
#: Mobipocket / KF8 container magic ("BOOKMOBI" at offset 60, or "TPZ" Topaz).
_MOBI_AT_60 = b"BOOKMOBI"
_PALMDB_TYPE_OFF = 60
#: Heuristic: a control-char-light, mostly-printable byte run reads as plain text.
_TEXT_CONTROL_BUDGET = 0.10


class SourceFormat(enum.StrEnum):
    """The normalised identity of an uploaded source byte-stream."""

    PDF = "pdf"
    EPUB = "epub"
    TXT = "txt"
    MARKDOWN = "markdown"
    HTML = "html"
    DOCX = "docx"
    MOBI = "mobi"
    UNKNOWN = "unknown"


class UnsupportedFormatError(ValueError):
    """Raised when an upload cannot be normalised to PDF (with a user-facing hint)."""

    def __init__(self, fmt: SourceFormat, message: str) -> None:
        super().__init__(message)
        self.format = fmt


@dataclass(frozen=True, slots=True)
class NormalizedSource:
    """A funnel result: PDF bytes plus the detected origin format + metadata.

    ``pdf_bytes`` always feeds the §9.1 extract step. ``source_format`` records
    what the user actually uploaded (for telemetry / provenance). ``title`` /
    ``author`` are best-effort document metadata used only when the caller has
    not supplied them.
    """

    pdf_bytes: bytes
    source_format: SourceFormat
    title: str | None = None
    author: str | None = None


# --------------------------------------------------------------------------- #
# Detection (content-magic, never the spoofable content-type)
# --------------------------------------------------------------------------- #


def _looks_like_docx(data: bytes) -> bool:
    """A DOCX is an OOXML ZIP containing ``word/document.xml``."""
    if data[:4] != _ZIP_MAGIC:
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            return "word/document.xml" in names and "[Content_Types].xml" in names
    except (zipfile.BadZipFile, OSError):
        return False


def _looks_like_mobi(data: bytes) -> bool:
    """Detect a Mobipocket/KF8 (``.mobi``/``.azw``/``.azw3``) PalmDB container."""
    if len(data) < _PALMDB_TYPE_OFF + 8:
        return False
    return data[_PALMDB_TYPE_OFF : _PALMDB_TYPE_OFF + 8] == _MOBI_AT_60


def _looks_like_text(data: bytes) -> bool:
    """Whether a head of ``data`` decodes as printable text (UTF-8/Latin-1)."""
    head = data[:4096]
    if not head:
        return False
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = head.decode("latin-1")
        except UnicodeDecodeError:
            return False
    if "\x00" in text:
        return False
    control = sum(1 for ch in text if ord(ch) < 9 or (13 < ord(ch) < 32))
    return control <= len(text) * _TEXT_CONTROL_BUDGET


_MD_MARKERS = re.compile(r"(^|\n)\s{0,3}(#{1,6}\s|[*\-+]\s|\d+\.\s|>\s|```)")
_HTML_MARKERS = re.compile(r"<(html|body|div|p|h[1-6]|br|span|article)\b", re.IGNORECASE)


def detect_format(data: bytes) -> SourceFormat:
    """Classify an uploaded byte-stream by content magic + light heuristics.

    Order matters: binary container signatures are unambiguous and checked first;
    the text family (HTML / Markdown / plain) is disambiguated only once the
    bytes are known to be printable text.
    """
    if data[:1024].lstrip().startswith(_PDF_MAGIC):
        return SourceFormat.PDF
    if data[:4] == _ZIP_MAGIC:
        if looks_like_epub(data):
            return SourceFormat.EPUB
        if _looks_like_docx(data):
            return SourceFormat.DOCX
        return SourceFormat.UNKNOWN
    if _looks_like_mobi(data):
        return SourceFormat.MOBI
    if _looks_like_text(data):
        try:
            text = data[:8192].decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - already validated as text-ish
            text = ""
        if _HTML_MARKERS.search(text):
            return SourceFormat.HTML
        if _MD_MARKERS.search(text):
            return SourceFormat.MARKDOWN
        return SourceFormat.TXT
    return SourceFormat.UNKNOWN


# --------------------------------------------------------------------------- #
# Tiny stdlib Markdown → HTML (headings, emphasis, lists, paragraphs)
# --------------------------------------------------------------------------- #

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_BULLET = re.compile(r"^\s*[*\-+]\s+(.*)$")
_MD_ORDERED = re.compile(r"^\s*\d+\.\s+(.*)$")
_MD_FENCE = re.compile(r"^\s*```")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _md_inline(text: str) -> str:
    """Escape then apply inline Markdown (bold/italic/code/link) → safe HTML."""
    out = _html.escape(text, quote=False)
    out = _MD_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _MD_BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", out)
    out = _MD_ITALIC.sub(lambda m: f"<i>{m.group(1)}</i>", out)
    out = _MD_LINK.sub(lambda m: f"<u>{m.group(1)}</u>", out)
    return out


def markdown_to_html(md: str) -> str:
    """Minimal, dependency-free Markdown→HTML good enough for layout to PDF.

    Handles ATX headings, unordered/ordered lists, fenced code blocks, blank-line
    paragraphs and inline bold/italic/code/links. Anything it does not recognise
    falls through as an escaped paragraph, so no input is ever lost.
    """
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    list_items: list[str] = []
    list_tag: str | None = None
    para: list[str] = []
    in_code = False
    code_buf: list[str] = []

    def flush_para() -> None:
        if para:
            blocks.append(f"<p>{_md_inline(' '.join(para))}</p>")
            para.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_items and list_tag:
            inner = "".join(f"<li>{it}</li>" for it in list_items)
            blocks.append(f"<{list_tag}>{inner}</{list_tag}>")
        list_items.clear()
        list_tag = None

    for raw in lines:
        if _MD_FENCE.match(raw):
            if in_code:
                blocks.append(f"<pre>{_html.escape(chr(10).join(code_buf))}</pre>")
                code_buf.clear()
                in_code = False
            else:
                flush_para()
                flush_list()
                in_code = True
            continue
        if in_code:
            code_buf.append(raw)
            continue

        heading = _MD_HEADING.match(raw)
        if heading:
            flush_para()
            flush_list()
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{_md_inline(heading.group(2).strip())}</h{level}>")
            continue

        bullet = _MD_BULLET.match(raw)
        ordered = _MD_ORDERED.match(raw)
        if bullet or ordered:
            flush_para()
            tag = "ul" if bullet else "ol"
            if list_tag != tag:
                flush_list()
                list_tag = tag
            item = (bullet or ordered).group(1).strip()  # type: ignore[union-attr]
            list_items.append(_md_inline(item))
            continue

        if not raw.strip():
            flush_para()
            flush_list()
            continue

        flush_list()
        para.append(raw.strip())

    if in_code and code_buf:
        blocks.append(f"<pre>{_html.escape(chr(10).join(code_buf))}</pre>")
    flush_para()
    flush_list()
    return "<html><body>" + "".join(blocks) + "</body></html>"


def text_to_html(text: str) -> str:
    """Wrap plain text in HTML, one ``<p>`` per blank-line-separated block."""
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", normalised)
    paras = [
        f"<p>{_html.escape(block.strip())}</p>"
        for block in blocks
        if block.strip()
    ]
    if not paras:
        paras = ["<p></p>"]
    return "<html><body>" + "".join(paras) + "</body></html>"


# --------------------------------------------------------------------------- #
# DOCX → HTML (stdlib only)
# --------------------------------------------------------------------------- #

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
_DC = "{http://purl.org/dc/elements/1.1/}"
#: Word style ids that map to an HTML heading level.
_HEADING_RE = re.compile(r"heading\s*([1-6])", re.IGNORECASE)


def _docx_paragraph_text(p: ET.Element) -> str:
    """Concatenate the visible text runs of one ``<w:p>`` paragraph."""
    parts: list[str] = []
    for node in p.iter():
        if node.tag == f"{_W}t":
            parts.append(node.text or "")
        elif node.tag in (f"{_W}tab",):
            parts.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append(" ")
    return "".join(parts)


def _docx_heading_level(p: ET.Element) -> int | None:
    """Return the HTML heading level (1-6) for a paragraph, or ``None``."""
    ppr = p.find(f"{_W}pPr")
    if ppr is None:
        return None
    style = ppr.find(f"{_W}pStyle")
    if style is None:
        return None
    val = style.get(f"{_W}val") or ""
    match = _HEADING_RE.search(val)
    if match:
        return int(match.group(1))
    if val.lower() in ("title",):
        return 1
    return None


def docx_to_html(data: bytes) -> tuple[str, str | None, str | None]:
    """Convert DOCX ``data`` → ``(html, title, author)`` using only the stdlib.

    Reads ``word/document.xml`` for the body paragraphs (mapping Word heading
    styles to ``<h1..6>``) and ``docProps/core.xml`` for ``dc:title`` /
    ``dc:creator``. Raises :class:`UnsupportedFormatError` on a malformed archive.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            document = zf.read("word/document.xml")
            core = zf.read("docProps/core.xml") if "docProps/core.xml" in zf.namelist() else None
    except (zipfile.BadZipFile, OSError, KeyError) as exc:
        raise UnsupportedFormatError(
            SourceFormat.DOCX, "file is not a readable DOCX document"
        ) from exc

    try:
        root = ET.fromstring(document)
    except ET.ParseError as exc:
        raise UnsupportedFormatError(
            SourceFormat.DOCX, "DOCX document.xml is malformed"
        ) from exc

    body = root.find(f"{_W}body")
    blocks: list[str] = []
    if body is not None:
        for para in body.findall(f"{_W}p"):
            text = _docx_paragraph_text(para).strip()
            if not text:
                continue
            level = _docx_heading_level(para)
            safe = _html.escape(text)
            if level:
                blocks.append(f"<h{level}>{safe}</h{level}>")
            else:
                blocks.append(f"<p>{safe}</p>")
    if not blocks:
        blocks = ["<p></p>"]

    title = author = None
    if core is not None:
        try:
            cp = ET.fromstring(core)
            t = cp.find(f"{_DC}title")
            a = cp.find(f"{_DC}creator")
            title = (t.text or "").strip() or None if t is not None else None
            author = (a.text or "").strip() or None if a is not None else None
        except ET.ParseError:
            pass

    return "<html><body>" + "".join(blocks) + "</body></html>", title, author


# --------------------------------------------------------------------------- #
# HTML → PDF (the shared layout primitive)
# --------------------------------------------------------------------------- #

#: A4-ish portrait at a comfortable reading measure; matches typical reflow output.
_PAGE_RECT = fitz.paper_rect("letter")
_MARGIN = 48.0
#: Hard ceiling on rendered pages so a pathological input cannot DoS extraction;
#: the API page cap (MAX_INGEST_PAGES) is the real limiter, this is a safety net.
_MAX_STORY_PAGES = 5000

_DEFAULT_CSS = (
    "body{font-family:serif;font-size:11pt;line-height:1.5;}"
    "h1{font-size:20pt;margin:18pt 0 8pt;}"
    "h2{font-size:16pt;margin:14pt 0 6pt;}"
    "h3{font-size:13pt;margin:12pt 0 5pt;}"
    "p{margin:0 0 8pt;}"
    "pre,code{font-family:monospace;font-size:10pt;}"
    "li{margin:0 0 4pt;}"
)


def html_to_pdf_bytes(html: str, *, css: str | None = None) -> bytes:
    """Lay out an HTML document to PDF bytes via PyMuPDF ``Story`` (reflowed pages).

    The produced PDF has real, selectable text in reading order, so the §9.1
    extract step gets per-word boxes exactly as it does for a native PDF. Pages
    are produced until the Story reports it has placed all content (or the
    safety ceiling is hit).
    """
    story = fitz.Story(html=html, user_css=css or _DEFAULT_CSS)
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    where = _PAGE_RECT + (_MARGIN, _MARGIN, -_MARGIN, -_MARGIN)
    more = 1
    pages = 0
    while more and pages < _MAX_STORY_PAGES:
        device = writer.begin_page(_PAGE_RECT)
        more, _ = story.place(where)
        story.draw(device)
        writer.end_page()
        pages += 1
    writer.close()
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# The funnel
# --------------------------------------------------------------------------- #


def _decode_text(data: bytes) -> str:
    """Decode source bytes as UTF-8 (BOM-aware) with a Latin-1 fallback."""
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def normalize_to_pdf(data: bytes, *, fmt: SourceFormat | None = None) -> NormalizedSource:
    """Normalise any supported upload to :class:`NormalizedSource` (PDF bytes).

    Args:
        data: the raw uploaded bytes.
        fmt: an optional pre-detected format (skips re-detection); when ``None``
            the format is detected from content magic via :func:`detect_format`.

    Raises:
        UnsupportedFormatError: for MOBI/AZW3 (rejected with a convert-to-EPUB
            hint) and for anything unrecognised.
    """
    fmt = fmt or detect_format(data)

    if fmt is SourceFormat.PDF:
        return NormalizedSource(pdf_bytes=data, source_format=fmt)

    if fmt is SourceFormat.EPUB:
        return NormalizedSource(pdf_bytes=epub_to_pdf_bytes(data), source_format=fmt)

    if fmt is SourceFormat.DOCX:
        html, title, author = docx_to_html(data)
        return NormalizedSource(
            pdf_bytes=html_to_pdf_bytes(html),
            source_format=fmt,
            title=title,
            author=author,
        )

    if fmt is SourceFormat.MARKDOWN:
        html = markdown_to_html(_decode_text(data))
        return NormalizedSource(pdf_bytes=html_to_pdf_bytes(html), source_format=fmt)

    if fmt is SourceFormat.HTML:
        return NormalizedSource(
            pdf_bytes=html_to_pdf_bytes(_sanitize_html(_decode_text(data))), source_format=fmt
        )

    if fmt is SourceFormat.TXT:
        html = text_to_html(_decode_text(data))
        return NormalizedSource(pdf_bytes=html_to_pdf_bytes(html), source_format=fmt)

    if fmt is SourceFormat.MOBI:
        raise UnsupportedFormatError(
            SourceFormat.MOBI,
            "MOBI/AZW3 is not supported directly; please convert it to EPUB "
            "(e.g. with Calibre) and re-upload.",
        )

    raise UnsupportedFormatError(
        SourceFormat.UNKNOWN,
        "file is not a supported format (PDF, EPUB, DOCX, Markdown, HTML, or TXT)",
    )


# Tags PyMuPDF's HTML engine cannot lay out / that could carry script; we strip
# them and keep the textual body so an uploaded web page still imports cleanly.
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|head|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _sanitize_html(html: str) -> str:
    """Strip script/style/head/comments from uploaded HTML before layout."""
    cleaned = _COMMENT_RE.sub("", html)
    cleaned = _SCRIPT_STYLE_RE.sub("", cleaned)
    return cleaned


# --------------------------------------------------------------------------- #
# Convenience: filename-extension hint (used only to disambiguate, never trust)
# --------------------------------------------------------------------------- #

_EXT_HINTS: dict[str, SourceFormat] = {
    ".pdf": SourceFormat.PDF,
    ".epub": SourceFormat.EPUB,
    ".txt": SourceFormat.TXT,
    ".text": SourceFormat.TXT,
    ".md": SourceFormat.MARKDOWN,
    ".markdown": SourceFormat.MARKDOWN,
    ".mdown": SourceFormat.MARKDOWN,
    ".html": SourceFormat.HTML,
    ".htm": SourceFormat.HTML,
    ".docx": SourceFormat.DOCX,
    ".mobi": SourceFormat.MOBI,
    ".azw": SourceFormat.MOBI,
    ".azw3": SourceFormat.MOBI,
}


def format_from_extension(filename: str | None) -> SourceFormat:
    """Best-effort format hint from a filename extension (``UNKNOWN`` if none)."""
    if not filename:
        return SourceFormat.UNKNOWN
    ext = posixpath.splitext(filename.lower())[1]
    return _EXT_HINTS.get(ext, SourceFormat.UNKNOWN)


__all__ = [
    "NormalizedSource",
    "SourceFormat",
    "UnsupportedFormatError",
    "detect_format",
    "docx_to_html",
    "format_from_extension",
    "html_to_pdf_bytes",
    "markdown_to_html",
    "normalize_to_pdf",
    "text_to_html",
]
