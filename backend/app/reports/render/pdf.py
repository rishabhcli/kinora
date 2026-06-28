"""PDF renderer — paginated, themed output via PyMuPDF (the existing dependency).

A small flowing layout engine: a :class:`_Cursor` tracks the current page + y
position; each block measures its own height, asks the cursor for room (starting
a new page when it would overflow), and draws itself. Charts are rasterised from
the same SVG the HTML embeds (``fitz.open("svg", …)`` → PNG → ``insert_image``)
so both formats show identical visuals with **no extra dependency**.

The renderer is deterministic — PyMuPDF lays text out the same way every run, and
we inject the document title/subject into the PDF metadata but pin the producer
so the bytes are reproducible enough for a structural golden check (the golden
tests assert on text content + page count, not a raw byte hash, since PDF streams
embed library-version noise).

Colors come from the brand palette (parsed to ``(r,g,b)`` floats); the type
scale drives font sizes. Built-in PDF fonts (``helv``/``helv-bold``) keep the
output font-embed-free and tiny.
"""

from __future__ import annotations

import fitz  # PyMuPDF

from app.reports.charts import render_chart
from app.reports.model import (
    Badge,
    BadgeTone,
    Block,
    Callout,
    CalloutTone,
    Chart,
    Divider,
    Heading,
    KeyValue,
    Paragraph,
    Report,
    Section,
    Spacer,
    Table,
)
from app.reports.theme import Brand, hex_to_rgb

# A4 portrait, in points.
_PAGE_W = 595.0
_PAGE_H = 842.0
_MARGIN_X = 48.0
_MARGIN_TOP = 54.0
_MARGIN_BOTTOM = 54.0
_CONTENT_W = _PAGE_W - 2 * _MARGIN_X

# PyMuPDF base-14 font names: ``helv`` = Helvetica, ``hebo`` = Helvetica-Bold.
_FONT = "helv"
_FONT_BOLD = "hebo"

#: Render charts at this scale so they're crisp when scaled into the PDF.
_CHART_SCALE = 2.0


class _Cursor:
    """Tracks the current page + vertical write position across the document."""

    def __init__(self, doc: fitz.Document, brand: Brand) -> None:
        self.doc = doc
        self.brand = brand
        self.page: fitz.Page = doc.new_page(width=_PAGE_W, height=_PAGE_H)
        self.y = _MARGIN_TOP
        self.page_no = 1
        self._paint_bg(self.page)

    def _paint_bg(self, page: fitz.Page) -> None:
        page.draw_rect(
            fitz.Rect(0, 0, _PAGE_W, _PAGE_H),
            color=None,
            fill=hex_to_rgb(self.brand.palette.background),
        )

    @property
    def bottom(self) -> float:
        return _PAGE_H - _MARGIN_BOTTOM

    def ensure(self, needed: float) -> None:
        """Start a new page if ``needed`` points won't fit below the cursor."""
        if self.y + needed > self.bottom:
            self.new_page()

    def new_page(self) -> None:
        self._footer(self.page)
        self.page = self.doc.new_page(width=_PAGE_W, height=_PAGE_H)
        self.page_no += 1
        self._paint_bg(self.page)
        self.y = _MARGIN_TOP

    def _footer(self, page: fitz.Page) -> None:
        pal = self.brand.palette
        page.draw_line(
            fitz.Point(_MARGIN_X, self.bottom + 14),
            fitz.Point(_PAGE_W - _MARGIN_X, self.bottom + 14),
            color=hex_to_rgb(pal.border),
            width=0.5,
        )
        page.insert_text(
            fitz.Point(_MARGIN_X, self.bottom + 28),
            f"{self.brand.name} — {self.brand.tagline}",
            fontsize=8,
            fontname=_FONT,
            color=hex_to_rgb(pal.text_muted),
        )
        page.insert_text(
            fitz.Point(_PAGE_W - _MARGIN_X - 20, self.bottom + 28),
            str(self.page_no),
            fontsize=8,
            fontname=_FONT,
            color=hex_to_rgb(pal.text_muted),
        )

    def finish(self) -> None:
        self._footer(self.page)


def _wrapped_height(text: str, width: float, fontsize: float, *, lh: float = 1.4) -> float:
    """Estimate the height needed to wrap ``text`` in ``width`` at ``fontsize``.

    Greedy word wrap using PyMuPDF's text-length metric — the same algorithm the
    textbox uses — so pagination matches what gets drawn.
    """
    if not text:
        return fontsize * lh
    line_h = fontsize * lh
    lines = 0
    for paragraph in text.split("\n"):
        cur = ""
        for word in paragraph.split(" "):
            trial = f"{cur} {word}".strip()
            if cur and fitz.get_text_length(trial, fontname=_FONT, fontsize=fontsize) > width:
                lines += 1
                cur = word
            else:
                cur = trial
        lines += 1
    return lines * line_h


def _text(
    cursor: _Cursor,
    text: str,
    *,
    size: float,
    color: tuple[float, float, float],
    bold: bool = False,
    width: float | None = None,
    lh: float = 1.4,
    x: float | None = None,
) -> None:
    """Draw a wrapped run of text and advance the cursor by its height."""
    w = width if width is not None else _CONTENT_W
    h = _wrapped_height(text, w, size, lh=lh)
    cursor.ensure(h)
    left = x if x is not None else _MARGIN_X
    rect = fitz.Rect(left, cursor.y, left + w, cursor.y + h + size)
    cursor.page.insert_textbox(
        rect,
        text,
        fontsize=size,
        fontname=_FONT_BOLD if bold else _FONT,
        color=color,
        lineheight=lh,
    )
    cursor.y += h


def _cover(cursor: _Cursor, report: Report) -> None:
    brand = cursor.brand
    pal = brand.palette
    meta = report.meta
    # Accent band.
    cursor.page.draw_rect(
        fitz.Rect(_MARGIN_X, cursor.y, _MARGIN_X + 46, cursor.y + 46),
        color=None,
        fill=hex_to_rgb(pal.accent),
        radius=0.22,
    )
    cursor.page.insert_text(
        fitz.Point(_MARGIN_X + 12, cursor.y + 30),
        "K",
        fontsize=26,
        fontname=_FONT_BOLD,
        color=(1, 1, 1),
    )
    cursor.page.insert_text(
        fitz.Point(_MARGIN_X + 60, cursor.y + 12),
        f"{brand.name.upper()} · {brand.tagline.upper()}",
        fontsize=8,
        fontname=_FONT,
        color=hex_to_rgb(pal.text_muted),
    )
    title_x = _MARGIN_X + 60
    title_w = _PAGE_W - _MARGIN_X - title_x
    title_size = brand.type_scale.title
    title_h = _wrapped_height(meta.title, title_w, title_size, lh=1.1)
    title_top = cursor.y + 16
    cursor.page.insert_textbox(
        fitz.Rect(title_x, title_top, _PAGE_W - _MARGIN_X, title_top + title_h + title_size),
        meta.title,
        fontsize=title_size,
        fontname=_FONT_BOLD,
        color=hex_to_rgb(pal.heading),
        lineheight=1.1,
    )
    cursor.y += max(56.0, 16 + title_h + 8)
    if meta.subtitle:
        _text(
            cursor,
            meta.subtitle,
            size=brand.type_scale.subtitle,
            color=hex_to_rgb(pal.text_muted),
        )
    cursor.y += 8
    cursor.page.draw_line(
        fitz.Point(_MARGIN_X, cursor.y),
        fitz.Point(_PAGE_W - _MARGIN_X, cursor.y),
        color=hex_to_rgb(pal.border),
        width=0.8,
    )
    cursor.y += 18


def _draw_keyvalue(cursor: _Cursor, block: KeyValue) -> None:
    brand = cursor.brand
    pal = brand.palette
    cols = max(1, block.columns)
    gap = 12.0
    cell_w = (_CONTENT_W - gap * (cols - 1)) / cols
    cell_h = 56.0
    items = list(block.items)
    for row_start in range(0, len(items), cols):
        row = items[row_start : row_start + cols]
        cursor.ensure(cell_h + 8)
        for i, item in enumerate(row):
            cx = _MARGIN_X + i * (cell_w + gap)
            rect = fitz.Rect(cx, cursor.y, cx + cell_w, cursor.y + cell_h)
            border = pal.accent if item.emphasis else pal.border
            cursor.page.draw_rect(
                rect,
                color=hex_to_rgb(border),
                fill=hex_to_rgb(pal.surface),
                width=1.0,
                radius=0.1,
            )
            cursor.page.insert_textbox(
                fitz.Rect(cx + 12, cursor.y + 10, cx + cell_w - 8, cursor.y + 36),
                item.stat.text(),
                fontsize=brand.type_scale.stat,
                fontname=_FONT_BOLD,
                color=hex_to_rgb(pal.heading),
            )
            cursor.page.insert_textbox(
                fitz.Rect(cx + 12, cursor.y + 38, cx + cell_w - 8, cursor.y + cell_h - 4),
                item.label.upper(),
                fontsize=brand.type_scale.stat_label,
                fontname=_FONT,
                color=hex_to_rgb(pal.text_muted),
            )
        cursor.y += cell_h + 8


def _draw_table(cursor: _Cursor, block: Table) -> None:
    brand = cursor.brand
    pal = brand.palette
    size = brand.type_scale.small
    if block.caption:
        _text(cursor, block.caption, size=size, color=hex_to_rgb(pal.text_muted))
        cursor.y += 2
    n = len(block.columns)
    col_w = _CONTENT_W / n if n else _CONTENT_W
    row_h = size * 2.0
    pad = 6.0

    def cell(
        x: float,
        y: float,
        text: str,
        align: str,
        bold: bool,
        color: tuple[float, float, float],
    ) -> None:
        rect = fitz.Rect(x + pad, y, x + col_w - pad, y + row_h)
        a = {"left": 0, "center": 1, "right": 2}[align]
        cursor.page.insert_textbox(
            rect, text, fontsize=size, fontname=_FONT_BOLD if bold else _FONT,
            color=color, align=a,
        )

    # Header.
    cursor.ensure(row_h * 2)
    hy = cursor.y
    for ci, c in enumerate(block.columns):
        cell(_MARGIN_X + ci * col_w, hy + 2, c.label.upper(), c.alignment().value, True,
             hex_to_rgb(pal.text_muted))
    cursor.y += row_h
    cursor.page.draw_line(
        fitz.Point(_MARGIN_X, cursor.y), fitz.Point(_PAGE_W - _MARGIN_X, cursor.y),
        color=hex_to_rgb(pal.border), width=0.8,
    )
    # Rows.
    for row in block.rows:
        cursor.ensure(row_h + 2)
        ry = cursor.y + 2
        for ci, c in enumerate(block.columns):
            cell(_MARGIN_X + ci * col_w, ry, row.get(c.key, ""), c.alignment().value, False,
                 hex_to_rgb(pal.text))
        cursor.y += row_h
        cursor.page.draw_line(
            fitz.Point(_MARGIN_X, cursor.y), fitz.Point(_PAGE_W - _MARGIN_X, cursor.y),
            color=hex_to_rgb(pal.border), width=0.4,
        )
    # Total footer.
    if block.total_row is not None:
        cursor.ensure(row_h + 2)
        ry = cursor.y + 2
        for ci, c in enumerate(block.columns):
            cell(_MARGIN_X + ci * col_w, ry, block.total_row.get(c.key, ""),
                 c.alignment().value, True, hex_to_rgb(pal.heading))
        cursor.y += row_h
    cursor.y += 6


def _draw_chart(cursor: _Cursor, block: Chart) -> None:
    brand = cursor.brand
    svg = render_chart(block, brand, width=int(_CONTENT_W))
    try:
        svg_doc = fitz.open("svg", svg.encode("utf-8"))
        pix = svg_doc[0].get_pixmap(matrix=fitz.Matrix(_CHART_SCALE, _CHART_SCALE), alpha=False)
        png = pix.tobytes("png")
    except Exception:  # pragma: no cover - degrade if the SVG can't rasterise
        return
    draw_h = block.height
    cursor.ensure(draw_h + 12)
    rect = fitz.Rect(_MARGIN_X, cursor.y, _MARGIN_X + _CONTENT_W, cursor.y + draw_h)
    # Card background behind the chart.
    cursor.page.draw_rect(
        rect, color=hex_to_rgb(brand.palette.border),
        fill=hex_to_rgb(brand.palette.surface), width=1.0, radius=0.04,
    )
    cursor.page.insert_image(rect, stream=png, keep_proportion=True)
    cursor.y += draw_h + 10


_TONE_COLOR = {
    CalloutTone.INFO: "info",
    CalloutTone.SUCCESS: "success",
    CalloutTone.WARNING: "warning",
    CalloutTone.DANGER: "danger",
    CalloutTone.NEUTRAL: "text_muted",
}
_BADGE_FILL = {
    BadgeTone.INFO: "info",
    BadgeTone.SUCCESS: "success",
    BadgeTone.WARNING: "warning",
    BadgeTone.DANGER: "danger",
    BadgeTone.ACCENT: "accent",
    BadgeTone.NEUTRAL: "surface_alt",
}


def _draw_callout(cursor: _Cursor, block: Callout) -> None:
    brand = cursor.brand
    pal = brand.palette
    tone = getattr(pal, _TONE_COLOR[block.tone])
    inner_w = _CONTENT_W - 24
    body_h = _wrapped_height(block.text, inner_w, brand.type_scale.body)
    title_h = brand.type_scale.h4 * 1.4 if block.title else 0.0
    box_h = body_h + title_h + 18
    cursor.ensure(box_h + 6)
    top = cursor.y
    cursor.page.draw_rect(
        fitz.Rect(_MARGIN_X, top, _PAGE_W - _MARGIN_X, top + box_h),
        color=None, fill=hex_to_rgb(pal.surface), radius=0.05,
    )
    cursor.page.draw_rect(
        fitz.Rect(_MARGIN_X, top, _MARGIN_X + 4, top + box_h),
        color=None, fill=hex_to_rgb(tone),
    )
    cursor.y = top + 9
    if block.title:
        _text(cursor, block.title, size=brand.type_scale.h4, color=hex_to_rgb(pal.heading),
              bold=True, width=inner_w, x=_MARGIN_X + 14)
    _text(cursor, block.text, size=brand.type_scale.body, color=hex_to_rgb(pal.text),
          width=inner_w, x=_MARGIN_X + 14)
    cursor.y = top + box_h + 8


def _draw_badge(cursor: _Cursor, block: Badge) -> None:
    brand = cursor.brand
    pal = brand.palette
    fill = getattr(pal, _BADGE_FILL[block.tone])
    size = brand.type_scale.small
    w = fitz.get_text_length(block.text, fontname=_FONT_BOLD, fontsize=size) + 22
    h = size + 10
    cursor.ensure(h + 6)
    rect = fitz.Rect(_MARGIN_X, cursor.y, _MARGIN_X + w, cursor.y + h)
    cursor.page.draw_rect(rect, color=None, fill=hex_to_rgb(fill), radius=0.5)
    fg = (1, 1, 1) if block.tone != BadgeTone.NEUTRAL else hex_to_rgb(pal.text)
    cursor.page.insert_textbox(
        rect, block.text, fontsize=size, fontname=_FONT_BOLD, color=fg, align=1,
    )
    cursor.y += h + 8


def _draw_block(cursor: _Cursor, block: Block) -> None:
    brand = cursor.brand
    pal = brand.palette
    if isinstance(block, Heading):
        cursor.y += 6
        _text(cursor, block.text, size=brand.type_scale.heading_size(block.level),
              color=hex_to_rgb(pal.heading), bold=True)
        cursor.y += 4
    elif isinstance(block, Paragraph):
        color = pal.text_muted if block.muted else pal.text
        _text(cursor, block.text, size=brand.type_scale.body, color=hex_to_rgb(color))
        cursor.y += 4
    elif isinstance(block, KeyValue):
        _draw_keyvalue(cursor, block)
    elif isinstance(block, Table):
        _draw_table(cursor, block)
    elif isinstance(block, Chart):
        _draw_chart(cursor, block)
    elif isinstance(block, Callout):
        _draw_callout(cursor, block)
    elif isinstance(block, Badge):
        _draw_badge(cursor, block)
    elif isinstance(block, Divider):
        cursor.ensure(14)
        cursor.y += 6
        cursor.page.draw_line(
            fitz.Point(_MARGIN_X, cursor.y), fitz.Point(_PAGE_W - _MARGIN_X, cursor.y),
            color=hex_to_rgb(pal.border), width=0.6,
        )
        cursor.y += 8
    elif isinstance(block, Spacer):
        cursor.y += block.size


def _draw_section(cursor: _Cursor, section: Section) -> None:
    if section.page_break_before and cursor.page_no >= 1 and cursor.y > _MARGIN_TOP + 1:
        cursor.new_page()
    if section.title:
        cursor.y += 8
        _text(cursor, section.title, size=cursor.brand.type_scale.h1,
              color=hex_to_rgb(cursor.brand.palette.heading), bold=True)
        cursor.page.draw_line(
            fitz.Point(_MARGIN_X, cursor.y + 2), fitz.Point(_MARGIN_X + 60, cursor.y + 2),
            color=hex_to_rgb(cursor.brand.palette.accent), width=2.0,
        )
        cursor.y += 10
    for block in section.blocks:
        _draw_block(cursor, block)


def render_pdf(report: Report, brand: Brand) -> bytes:
    """Render a report to a paginated, themed PDF and return the bytes."""
    doc = fitz.open()
    try:
        doc.set_metadata(
            {
                "title": report.meta.title,
                "author": brand.name,
                "subject": report.meta.subtitle or report.meta.kind,
                "creator": f"{brand.name} Reports",
                "producer": f"{brand.name} Reports / PyMuPDF",
            }
        )
        cursor = _Cursor(doc, brand)
        _cover(cursor, report)
        for section in report.sections:
            _draw_section(cursor, section)
        cursor.finish()
        out: bytes = doc.tobytes(garbage=4, deflate=True)
        return out
    finally:
        doc.close()


__all__ = ["render_pdf"]
