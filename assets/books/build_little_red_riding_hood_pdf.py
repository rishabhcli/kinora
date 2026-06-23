#!/usr/bin/env python3
"""Build Kinora's second bundled public-domain book as a real, multi-page PDF.

Story: **"Little Red Riding Hood"** — an abridged retelling of the Brothers Grimm
fairy tale (*Rotkäppchen*, Kinder- und Hausmärchen, 1812). The tale and its 19th
century English translations are firmly in the **public domain**.

Like the Frog-King demo, this book is deliberately small and ingest-friendly:

* a title page + four story pages (five total),
* three principal characters for the canon — **Little Red Riding Hood**, the
  **Wolf**, and **Grandmother**,
* a real, selectable text layer on every page, and
* simple vector illustrations on every page plus one embedded raster cover.

Run::

    backend/.venv/bin/python assets/books/build_little_red_riding_hood_pdf.py
"""

from __future__ import annotations

import io
from pathlib import Path

import fitz  # PyMuPDF

HERE = Path(__file__).resolve().parent
OUT_PATH = HERE / "little_red_riding_hood.pdf"

PAGE_W, PAGE_H = 720.0, 960.0
MARGIN = 64.0

RED = (0.72, 0.12, 0.16)
RED_DK = (0.48, 0.08, 0.10)
WOLF = (0.38, 0.36, 0.34)
WOLF_DK = (0.22, 0.20, 0.18)
FOREST = (0.18, 0.40, 0.26)
FOREST_DK = (0.10, 0.26, 0.16)
SKY = (0.72, 0.84, 0.92)
INK = (0.13, 0.12, 0.16)
PARCHMENT = (0.97, 0.95, 0.89)
COTTAGE = (0.78, 0.62, 0.38)
COTTAGE_DK = (0.52, 0.38, 0.22)
WHITE = (0.96, 0.94, 0.90)

TITLE_FONT = "times-bold"
BODY_FONT = "times-roman"
ITALIC_FONT = "times-italic"


def _fill_background(page: fitz.Page, color: tuple[float, float, float]) -> None:
    page.draw_rect(page.rect, color=color, fill=color)


def _draw_forest_floor(page: fitz.Page, panel: fitz.Rect) -> None:
    page.draw_rect(panel, color=FOREST_DK, fill=FOREST, width=0)
    for x in range(int(panel.x0), int(panel.x1), 48):
        page.draw_line(
            fitz.Point(x, panel.y0 + 20),
            fitz.Point(x + 18, panel.y1 - 10),
            color=FOREST_DK,
            width=3,
        )


def _draw_tree(page: fitz.Page, cx: float, base_y: float, scale: float = 1.0) -> None:
    page.draw_rect(
        fitz.Rect(cx - 8 * scale, base_y - 90 * scale, cx + 8 * scale, base_y),
        color=FOREST_DK,
        fill=(0.42, 0.28, 0.16),
    )
    page.draw_circle(fitz.Point(cx, base_y - 110 * scale), 36 * scale, color=FOREST_DK, fill=FOREST)


def _draw_red_hood(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    """Simple girl figure with a red hooded cape."""
    page.draw_circle(
        fitz.Point(cx, cy - 40 * scale),
        18 * scale,
        color=(0.55, 0.38, 0.28),
        fill=(0.97, 0.82, 0.66),
        width=1.2,
    )
    hood = [
        fitz.Point(cx, cy - 58 * scale),
        fitz.Point(cx - 28 * scale, cy - 18 * scale),
        fitz.Point(cx + 28 * scale, cy - 18 * scale),
    ]
    page.draw_polyline(hood, color=RED_DK, fill=RED, width=1.5, closePath=True)
    page.draw_rect(
        fitz.Rect(cx - 22 * scale, cy - 18 * scale, cx + 22 * scale, cy + 48 * scale),
        color=RED_DK,
        fill=RED,
        width=1.2,
    )
    page.draw_rect(
        fitz.Rect(cx - 10 * scale, cy + 48 * scale, cx - 2 * scale, cy + 78 * scale),
        color=INK,
        fill=INK,
    )
    page.draw_rect(
        fitz.Rect(cx + 2 * scale, cy + 48 * scale, cx + 10 * scale, cy + 78 * scale),
        color=INK,
        fill=INK,
    )
    page.draw_circle(fitz.Point(cx - 28 * scale, cy + 8 * scale), 10 * scale, color=RED_DK, fill=RED)


def _draw_wolf(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    body = fitz.Rect(cx - 70 * scale, cy - 20 * scale, cx + 70 * scale, cy + 40 * scale)
    page.draw_oval(body, color=WOLF_DK, fill=WOLF, width=1.5)
    head = fitz.Rect(cx + 40 * scale, cy - 50 * scale, cx + 110 * scale, cy + 10 * scale)
    page.draw_oval(head, color=WOLF_DK, fill=WOLF, width=1.5)
    page.draw_circle(fitz.Point(cx + 78 * scale, cy - 18 * scale), 5 * scale, color=None, fill=INK)
    page.draw_circle(fitz.Point(cx + 98 * scale, cy - 18 * scale), 5 * scale, color=None, fill=INK)
    page.draw_polyline(
        [
            fitz.Point(cx + 118 * scale, cy - 8 * scale),
            fitz.Point(cx + 128 * scale, cy + 2 * scale),
            fitz.Point(cx + 108 * scale, cy + 2 * scale),
        ],
        color=WOLF_DK,
        fill=WOLF_DK,
        width=1.2,
        closePath=True,
    )


def _draw_cottage(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    w = 160 * scale
    h = 90 * scale
    body = fitz.Rect(cx - w / 2, cy, cx + w / 2, cy + h)
    page.draw_rect(body, color=COTTAGE_DK, fill=COTTAGE, width=1.5)
    roof = [
        fitz.Point(cx - w / 2 - 12 * scale, cy),
        fitz.Point(cx, cy - 70 * scale),
        fitz.Point(cx + w / 2 + 12 * scale, cy),
    ]
    page.draw_polyline(roof, color=COTTAGE_DK, fill=RED_DK, width=1.5, closePath=True)
    page.draw_rect(
        fitz.Rect(cx - 18 * scale, cy + 30 * scale, cx + 18 * scale, cy + h),
        color=COTTAGE_DK,
        fill=(0.35, 0.22, 0.12),
    )
    page.draw_rect(
        fitz.Rect(cx + 36 * scale, cy + 18 * scale, cx + 56 * scale, cy + 38 * scale),
        color=SKY,
        fill=SKY,
        width=1.2,
    )


def _draw_basket(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    page.draw_rect(
        fitz.Rect(cx - 24 * scale, cy, cx + 24 * scale, cy + 22 * scale),
        color=COTTAGE_DK,
        fill=COTTAGE,
        width=1.2,
    )
    page.draw_bezier(
        fitz.Point(cx - 18 * scale, cy),
        fitz.Point(cx - 18 * scale, cy - 28 * scale),
        fitz.Point(cx + 18 * scale, cy - 28 * scale),
        fitz.Point(cx + 18 * scale, cy),
        color=COTTAGE_DK,
        width=2,
    )


def _cover_raster(width: int = 520, height: int = 300) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (210, 228, 206))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, int(height * 0.55)], fill=(170, 206, 224))
    d.rectangle([0, int(height * 0.55), width, height], fill=(72, 120, 72))
    for x in (50, 180, 360, 470):
        d.rectangle([x - 5, 120, x + 5, 210], fill=(90, 60, 30))
        d.ellipse([x - 34, 70, x + 34, 138], fill=(50, 110, 60))
    # Red hood figure.
    hx, hy = int(width * 0.38), int(height * 0.62)
    d.ellipse([hx - 14, hy - 52, hx + 14, hy - 24], fill=(240, 200, 170))
    d.polygon([(hx, hy - 70), (hx - 26, hy - 22), (hx + 26, hy - 22)], fill=(170, 28, 36))
    d.rectangle([hx - 20, hy - 22, hx + 20, hy + 36], fill=(170, 28, 36))
    d.rectangle([hx + 18, hy - 6, hx + 34, hy + 16], fill=(170, 28, 36))
    # Basket.
    bx, by = int(width * 0.52), int(height * 0.72)
    d.rectangle([bx - 18, by, bx + 18, by + 16], fill=(150, 110, 60), outline=(90, 60, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _heading(page: fitz.Page, text: str, y: float) -> None:
    page.insert_textbox(
        fitz.Rect(MARGIN, y, PAGE_W - MARGIN, y + 44),
        text,
        fontname=TITLE_FONT,
        fontsize=24,
        color=INK,
        align=fitz.TEXT_ALIGN_LEFT,
    )


def _paragraph(page: fitz.Page, text: str, y: float, height: float = 200.0) -> None:
    page.insert_textbox(
        fitz.Rect(MARGIN, y, PAGE_W - MARGIN, y + height),
        text,
        fontname=BODY_FONT,
        fontsize=15,
        color=INK,
        align=fitz.TEXT_ALIGN_LEFT,
        lineheight=1.5,
    )


def _title_page(doc: fitz.Document) -> None:
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    _fill_background(page, PARCHMENT)
    page.insert_textbox(
        fitz.Rect(MARGIN, 120, PAGE_W - MARGIN, 230),
        "Little Red Riding Hood",
        fontname=TITLE_FONT,
        fontsize=40,
        color=INK,
        align=fitz.TEXT_ALIGN_CENTER,
    )
    page.insert_textbox(
        fitz.Rect(MARGIN, 232, PAGE_W - MARGIN, 300),
        "An abridged retelling of the Brothers Grimm fairy tale",
        fontname=ITALIC_FONT,
        fontsize=17,
        color=FOREST_DK,
        align=fitz.TEXT_ALIGN_CENTER,
    )
    page.insert_image(
        fitz.Rect(MARGIN + 30, 330, PAGE_W - MARGIN - 30, 640),
        stream=_cover_raster(),
        keep_proportion=True,
    )
    page.insert_textbox(
        fitz.Rect(MARGIN, 720, PAGE_W - MARGIN, 800),
        "Public domain · a Kinora demo book — watch the book.",
        fontname=BODY_FONT,
        fontsize=13,
        color=FOREST_DK,
        align=fitz.TEXT_ALIGN_CENTER,
    )


def _story_page(doc: fitz.Document, heading: str, body: str, illustrate) -> None:
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    _fill_background(page, PARCHMENT)
    _heading(page, heading, MARGIN)
    _paragraph(page, body, MARGIN + 56, height=300)
    panel = fitz.Rect(MARGIN, 470, PAGE_W - MARGIN, 860)
    page.draw_rect(panel, color=(0.80, 0.76, 0.66), fill=(0.93, 0.91, 0.84), width=1.2)
    illustrate(page, panel)


def build() -> Path:
    doc = fitz.open()
    doc.set_metadata(
        {
            "title": "Little Red Riding Hood (abridged)",
            "author": "Brothers Grimm (public domain) — abridged for Kinora",
            "subject": "Public-domain demo book for Kinora generation-on-scroll",
            "keywords": "public-domain, Grimm, fairy tale, Kinora demo",
        }
    )

    _title_page(doc)

    _story_page(
        doc,
        "1. Through the woods",
        (
            "Once upon a time there was a sweet little girl whom everyone loved. "
            "Her grandmother had made her a little red velvet cap, and because she "
            "would never wear anything else, she was called Little Red Riding Hood. "
            "One day her mother said, \"Take this cake and bottle of wine to your "
            "grandmother, who is ill. Go quietly and do not leave the path, for the "
            "woods are deep.\" Little Red Riding Hood promised, took her basket, and "
            "set out through the tall trees."
        ),
        lambda page, panel: (
            _draw_forest_floor(page, panel),
            _draw_tree(page, panel.x0 + 80, panel.y1 - 20, 1.0),
            _draw_tree(page, panel.x1 - 100, panel.y1 - 20, 1.1),
            _draw_red_hood(page, panel.x0 + 200, panel.y0 + 180, 1.1),
            _draw_basket(page, panel.x0 + 250, panel.y0 + 250, 1.0),
        ),
    )

    _story_page(
        doc,
        "2. The wolf on the path",
        (
            "Little Red Riding Hood had not gone far when a Wolf met her. He wished "
            "her good day, but thought to himself, \"What a tender young creature — "
            "what a nice meal she would make!\" He asked where she was going. "
            "\"To my grandmother, who lives in the wood,\" she answered. The Wolf "
            "walked a while beside her, then said, \"See how the flowers bloom! Why "
            "not gather a bouquet for your grandmother?\" While the child picked "
            "flowers, the Wolf ran straight to the grandmother's house."
        ),
        lambda page, panel: (
            _draw_forest_floor(page, panel),
            _draw_red_hood(page, panel.x0 + 120, panel.y0 + 190, 1.0),
            _draw_wolf(page, panel.x0 + 300, panel.y0 + 210, 0.9),
            _draw_tree(page, panel.x1 - 70, panel.y1 - 20, 0.9),
        ),
    )

    _story_page(
        doc,
        "3. At grandmother's door",
        (
            "The Wolf knocked at the cottage door. \"Who is there?\" called the "
            "grandmother. \"Little Red Riding Hood,\" answered the Wolf, disguising "
            "his voice. \"Pull the latch and come in.\" The Wolf entered, and the "
            "poor grandmother had no time to cry out before he swallowed her whole. "
            "Then he put on her cap, lay in her bed, and waited. Soon Little Red "
            "Riding Hood arrived with her basket and wondered why the door stood open."
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 220, panel.y0 + 120, 1.2),
            _draw_red_hood(page, panel.x0 + 90, panel.y0 + 220, 0.95),
            _draw_basket(page, panel.x0 + 130, panel.y0 + 280, 0.9),
        ),
    )

    _story_page(
        doc,
        "4. What big eyes you have",
        (
            "Inside the cottage Little Red Riding Hood drew near the bed. \"Grandmother, "
            "what big ears you have!\" \"All the better to hear you with, my child.\" "
            "\"Grandmother, what big eyes you have!\" \"All the better to see you with.\" "
            "\"Grandmother, what big teeth you have!\" \"All the better to eat you with!\" "
            "At that the Wolf sprang up — but just then a huntsman passing by heard the "
            "cry, rushed in, and rescued them both. Grandmother came out well, Little Red "
            "Riding Hood learned to keep to the path, and they ate the cake together."
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 240, panel.y0 + 80, 0.9),
            _draw_wolf(page, panel.x0 + 180, panel.y0 + 210, 0.75),
            _draw_red_hood(page, panel.x0 + 80, panel.y0 + 220, 0.85),
        ),
    )

    doc.save(str(OUT_PATH), deflate=True, garbage=4)
    doc.close()
    return OUT_PATH


def verify(path: Path) -> dict[str, object]:
    doc = fitz.open(str(path))
    try:
        num_pages = doc.page_count
        total_words = sum(len(page.get_text("words")) for page in doc)
        total_drawings = sum(len(page.get_drawings()) for page in doc)
        total_images = sum(len(page.get_images(full=True)) for page in doc)
        meta = dict(doc.metadata or {})
    finally:
        doc.close()

    assert num_pages >= 4, f"expected >= 4 pages, got {num_pages}"
    assert total_words >= 200, f"expected a real text layer, got {total_words} words"
    assert total_drawings >= 1, "expected vector illustrations"
    assert total_images >= 1, "expected an embedded raster cover"
    size_bytes = path.stat().st_size
    assert size_bytes < 5 * 1024 * 1024, f"PDF too large: {size_bytes} bytes"

    return {
        "path": str(path),
        "pages": num_pages,
        "words": total_words,
        "vector_drawings": total_drawings,
        "embedded_images": total_images,
        "size_kb": round(size_bytes / 1024, 1),
        "title": meta.get("title"),
        "author": meta.get("author"),
    }


def main() -> int:
    path = build()
    info = verify(path)
    print("Built + verified Little Red Riding Hood:")
    for key, value in info.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
