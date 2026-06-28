"""Render a :class:`NormalizedDocument` to the §9.1 ingest entry format (PDF).

The whole point of the integrations framework is to feed imported material into
the *unchanged* ingest pipeline, whose one public entry is
``ingest_pdf(book_id, pdf_bytes, ...)``. So a connector's normalized document is
turned into real PDF bytes here, using the very same PyMuPDF HTML→PDF path the
EPUB upload uses (``fitz.open(..., filetype="html").convert_to_pdf()``). The
result is a `%PDF-` document with selectable text, which means
``app.ingest.pdf_extract`` produces page images, text, and per-word boxes exactly
as it does for any other book.

The renderer is deterministic and fully offline — no network, no fonts beyond
PyMuPDF's built-ins — so it is trivially testable.
"""

from __future__ import annotations

from html import escape

import fitz  # PyMuPDF

from app.core.logging import get_logger
from app.integrations.errors import ConnectorError
from app.integrations.models import BlockKind, NormalizedBlock, NormalizedDocument

logger = get_logger("app.integrations.document")

#: Minimal print stylesheet. Kept inline so the renderer needs no asset files.
_STYLE = """
@page { margin: 64px 56px; }
body { font-family: Georgia, 'Times New Roman', serif; font-size: 13pt;
       line-height: 1.5; color: #111; }
h1 { font-size: 24pt; margin: 0 0 8px 0; }
h2 { font-size: 16pt; margin: 24px 0 4px 0; }
h3 { font-size: 13pt; margin: 18px 0 4px 0; font-variant: small-caps; }
p  { margin: 0 0 12px 0; text-align: justify; }
.byline { color: #555; font-style: italic; margin: 0 0 20px 0; }
blockquote { margin: 12px 24px; padding-left: 12px; border-left: 3px solid #999;
             color: #333; font-style: italic; }
.note { margin: 8px 24px; color: #444; font-size: 11pt; }
.cite { color: #777; font-size: 10pt; }
hr { border: none; border-top: 1px solid #bbb; margin: 20px 0; }
"""


def _block_html(block: NormalizedBlock) -> str:
    """Render one normalized block to a styled HTML fragment (escaped)."""
    if block.kind is BlockKind.DIVIDER:
        return "<hr/>"
    text = escape(block.text).replace("\n", "<br/>")
    cite = f'<div class="cite">{escape(block.cite)}</div>' if block.cite else ""
    if block.kind is BlockKind.HEADING:
        return f"<h2>{text}</h2>"
    if block.kind is BlockKind.SUBHEADING:
        return f"<h3>{text}</h3>"
    if block.kind is BlockKind.QUOTE:
        return f"<blockquote>{text}{cite}</blockquote>"
    if block.kind is BlockKind.NOTE:
        return f'<div class="note">{text}{cite}</div>'
    return f"<p>{text}</p>{cite}"


def render_html(doc: NormalizedDocument) -> str:
    """Render a normalized document to a self-contained HTML string.

    Exposed separately from :func:`render_pdf` so it can be asserted on in tests
    and reused by any future non-PDF surface (a preview pane, etc.).
    """
    head = f"<title>{escape(doc.title)}</title><style>{_STYLE}</style>"
    body_parts = [f"<h1>{escape(doc.title)}</h1>"]
    if doc.author:
        body_parts.append(f'<div class="byline">by {escape(doc.author)}</div>')
    body_parts.extend(_block_html(b) for b in doc.blocks)
    body = "".join(body_parts)
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"{head}</head><body>{body}</body></html>"
    )


def render_pdf(doc: NormalizedDocument) -> bytes:
    """Render a normalized document to PDF bytes (the ingest entry format).

    Args:
        doc: the connector-produced normalized document.

    Returns:
        ``%PDF-`` bytes with selectable text, ready to pass straight to
        ``app.ingest.ingest_pdf`` / ``Container.run_ingest``.

    Raises:
        ConnectorError: if the document is empty or PyMuPDF cannot serialise it.
    """
    if doc.is_empty():
        raise ConnectorError("cannot render an empty document to PDF")
    html = render_html(doc)
    try:
        story = fitz.open(stream=html.encode("utf-8"), filetype="html")
        try:
            pdf_bytes: bytes = story.convert_to_pdf()
        finally:
            story.close()
    except Exception as exc:  # noqa: BLE001 - any MuPDF failure => unusable doc
        raise ConnectorError(f"failed to render document to PDF: {exc}") from exc
    if not pdf_bytes[:5].startswith(b"%PDF-"):  # pragma: no cover - defensive
        raise ConnectorError("rendered output is not a valid PDF")
    logger.debug("integrations.render_pdf", title=doc.title, bytes=len(pdf_bytes))
    return pdf_bytes


__all__ = ["render_html", "render_pdf"]
