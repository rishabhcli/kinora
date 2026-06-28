"""Format-funnel unit tests — every input normalises to extractable PDF (§9.1).

Network-free, DB-free: these exercise the pure :mod:`app.ingest.formats`
conversions and assert each produces a PDF whose text PyMuPDF can read back, so
the downstream §9.1 extract path receives real, selectable words.
"""

from __future__ import annotations

import io
import zipfile

import fitz  # PyMuPDF
import pytest

from app.ingest.formats import (
    NormalizedSource,
    SourceFormat,
    UnsupportedFormatError,
    detect_format,
    docx_to_html,
    format_from_extension,
    html_to_pdf_bytes,
    markdown_to_html,
    normalize_to_pdf,
    text_to_html,
)
from tests.test_ingest_support import build_test_pdf

# --------------------------------------------------------------------------- #
# Fixtures: build the various formats in-memory
# --------------------------------------------------------------------------- #


def _pdf_text(data: bytes) -> str:
    with fitz.open(stream=data, filetype="pdf") as doc:
        return "\n".join(page.get_text("text") for page in doc)


def _build_docx(
    paragraphs: list[tuple[str, int | None]], *, title: str = "", author: str = ""
) -> bytes:
    """Build a minimal valid DOCX (OOXML ZIP) from (text, heading_level) tuples."""
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = []
    for text, level in paragraphs:
        ppr = f'<w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr>' if level else ""
        body.append(f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>')
    document = (
        f'<?xml version="1.0"?><w:document xmlns:w="{w}"><w:body>'
        + "".join(body)
        + "</w:body></w:document>"
    )
    core = (
        '<?xml version="1.0"?>'
        '<cp:coreProperties '
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>"
        "</cp:coreProperties>"
    )
    content_types = (
        '<?xml version="1.0"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        "</Types>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("word/document.xml", document)
        zf.writestr("docProps/core.xml", core)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def test_detect_pdf() -> None:
    assert detect_format(build_test_pdf(["hello world"])) is SourceFormat.PDF


def test_detect_plain_text() -> None:
    assert detect_format(b"Just some prose with no markup at all here.") is SourceFormat.TXT


def test_detect_markdown() -> None:
    assert detect_format(b"# Chapter One\n\nIt was a dark night.") is SourceFormat.MARKDOWN


def test_detect_html() -> None:
    assert detect_format(b"<html><body><p>Hi</p></body></html>") is SourceFormat.HTML


def test_detect_docx() -> None:
    data = _build_docx([("Hello", None)])
    assert detect_format(data) is SourceFormat.DOCX


def test_detect_mobi() -> None:
    palm = b"\x00" * 60 + b"BOOKMOBI" + b"\x00" * 16
    assert detect_format(palm) is SourceFormat.MOBI


def test_detect_unknown_binary() -> None:
    assert detect_format(b"\x00\x01\x02\xff\xfe random binary \x00\x00") is SourceFormat.UNKNOWN


def test_format_from_extension() -> None:
    assert format_from_extension("book.MD") is SourceFormat.MARKDOWN
    assert format_from_extension("paper.docx") is SourceFormat.DOCX
    assert format_from_extension("none") is SourceFormat.UNKNOWN
    assert format_from_extension(None) is SourceFormat.UNKNOWN


# --------------------------------------------------------------------------- #
# Markdown → HTML
# --------------------------------------------------------------------------- #


def test_markdown_to_html_structures() -> None:
    md = (
        "# Title\n\n"
        "Some **bold** and *italic* and `code`.\n\n"
        "- one\n- two\n\n"
        "1. first\n2. second\n\n"
        "```\nraw code\n```\n"
    )
    html = markdown_to_html(md)
    assert "<h1>Title</h1>" in html
    assert "<b>bold</b>" in html and "<i>italic</i>" in html and "<code>code</code>" in html
    assert "<ul><li>one</li><li>two</li></ul>" in html
    assert "<ol><li>first</li><li>second</li></ol>" in html
    assert "<pre>raw code</pre>" in html


def test_markdown_escapes_html() -> None:
    html = markdown_to_html("A <script>alert(1)</script> tag.")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_text_to_html_one_para_per_block() -> None:
    html = text_to_html("First block.\n\nSecond block.\n\n\nThird.")
    assert html.count("<p>") == 3


# --------------------------------------------------------------------------- #
# DOCX → HTML
# --------------------------------------------------------------------------- #


def test_docx_to_html_headings_and_meta() -> None:
    data = _build_docx(
        [("My Book", 1), ("A paragraph of body text.", None), ("Section", 2)],
        title="My Book",
        author="Jane Doe",
    )
    html, title, author = docx_to_html(data)
    assert "<h1>My Book</h1>" in html
    assert "<h2>Section</h2>" in html
    assert "<p>A paragraph of body text.</p>" in html
    assert title == "My Book"
    assert author == "Jane Doe"


def test_docx_malformed_raises() -> None:
    with pytest.raises(UnsupportedFormatError):
        docx_to_html(b"not a zip at all")


# --------------------------------------------------------------------------- #
# normalize_to_pdf — the funnel produces extractable PDFs
# --------------------------------------------------------------------------- #


def test_normalize_pdf_is_verbatim() -> None:
    pdf = build_test_pdf(["The quick brown fox jumps over the lazy dog repeatedly here."])
    out = normalize_to_pdf(pdf)
    assert out.source_format is SourceFormat.PDF
    assert out.pdf_bytes is pdf  # verbatim, no re-encode


def test_normalize_txt_to_readable_pdf() -> None:
    body = "Once upon a time there was a small village by a great river.\n\n" * 5
    out = normalize_to_pdf(body.encode("utf-8"))
    assert out.source_format is SourceFormat.TXT
    text = _pdf_text(out.pdf_bytes)
    assert "village" in text and "river" in text


def test_normalize_markdown_to_readable_pdf() -> None:
    md = "# The Tale\n\nA hero set out at dawn across the wide and windy moor.\n"
    out = normalize_to_pdf(md.encode("utf-8"))
    assert out.source_format is SourceFormat.MARKDOWN
    text = _pdf_text(out.pdf_bytes)
    assert "hero" in text and "moor" in text


def test_normalize_html_strips_script_and_reads_body() -> None:
    html = (
        "<html><head><style>p{color:red}</style></head><body>"
        "<script>evil()</script>"
        "<p>Visible narrative content about a brave knight.</p>"
        "</body></html>"
    )
    out = normalize_to_pdf(html.encode("utf-8"))
    assert out.source_format is SourceFormat.HTML
    text = _pdf_text(out.pdf_bytes)
    assert "knight" in text
    assert "evil" not in text


def test_normalize_docx_to_readable_pdf() -> None:
    data = _build_docx(
        [("Chapter", 1), ("The dragon slept beneath the lonely mountain for an age.", None)],
        title="Saga",
        author="A. Writer",
    )
    out = normalize_to_pdf(data)
    assert out.source_format is SourceFormat.DOCX
    assert out.title == "Saga" and out.author == "A. Writer"
    text = _pdf_text(out.pdf_bytes)
    assert "dragon" in text and "mountain" in text


def test_normalize_mobi_rejected_with_hint() -> None:
    palm = b"\x00" * 60 + b"BOOKMOBI" + b"\x00" * 16
    with pytest.raises(UnsupportedFormatError) as exc:
        normalize_to_pdf(palm)
    assert "EPUB" in str(exc.value)
    assert exc.value.format is SourceFormat.MOBI


def test_normalize_unknown_rejected() -> None:
    with pytest.raises(UnsupportedFormatError):
        normalize_to_pdf(b"\x00\x01\x02\xff garbage \x00")


def test_html_to_pdf_multipage_for_long_input() -> None:
    long_html = "<html><body>" + "".join(
        f"<p>Paragraph {i} carries several words to force the story across pages.</p>"
        for i in range(300)
    ) + "</body></html>"
    pdf = html_to_pdf_bytes(long_html)
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        assert doc.page_count > 1


def test_normalized_source_dataclass_shape() -> None:
    ns = NormalizedSource(pdf_bytes=b"%PDF-x", source_format=SourceFormat.PDF)
    assert ns.title is None and ns.author is None


# --------------------------------------------------------------------------- #
# EPUB funnel + text-encoding edges
# --------------------------------------------------------------------------- #


def test_detect_and_normalize_epub() -> None:
    from tests.test_ingest_support import build_test_epub

    epub = build_test_epub(["A clever fox crossed the wide windswept moor before the dawn."])
    assert detect_format(epub) is SourceFormat.EPUB
    out = normalize_to_pdf(epub)
    assert out.source_format is SourceFormat.EPUB
    text = _pdf_text(out.pdf_bytes)
    assert "fox" in text and "moor" in text


def test_txt_utf8_bom_is_stripped() -> None:
    data = b"\xef\xbb\xbf" + "A café by the river at noon.".encode()
    out = normalize_to_pdf(data)
    assert out.source_format is SourceFormat.TXT
    text = _pdf_text(out.pdf_bytes)
    assert "caf" in text  # accented char round-trips, BOM does not leak


def test_latin1_text_decodes() -> None:
    # 0xE9 is 'é' in Latin-1; not valid UTF-8 on its own.
    data = b"The caf\xe9 served warm bread every single morning without fail here."
    assert detect_format(data) is SourceFormat.TXT
    out = normalize_to_pdf(data)
    text = _pdf_text(out.pdf_bytes)
    assert "served" in text


def test_explicit_format_skips_detection() -> None:
    # Force TXT handling on bytes that would otherwise sniff as markdown.
    out = normalize_to_pdf(b"# Not a heading here", fmt=SourceFormat.TXT)
    assert out.source_format is SourceFormat.TXT
    text = _pdf_text(out.pdf_bytes)
    assert "#" in text  # treated as literal text, not a heading
