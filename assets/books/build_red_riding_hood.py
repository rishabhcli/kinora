#!/usr/bin/env python3
"""Build Kinora's second bundled public-domain demo book as a multi-page PDF.

Story: **"Little Red Riding Hood"** — an abridged retelling of the Brothers Grimm
fairy tale (*Rotkäppchen*, Kinder- und Hausmärchen, 1812). Public domain.

Five pages: title + four story beats, three principal characters (Red Riding Hood,
the Wolf, the Grandmother), selectable text, and simple vector illustrations.

Run::

    backend/.venv/bin/python assets/books/build_red_riding_hood.py

Writes ``assets/books/little_red_riding_hood.pdf``.
"""

from __future__ import annotations

import io
from pathlib import Path

import fitz  # PyMuPDF

HERE = Path(__file__).resolve().parent
OUT_PATH = HERE / "little_red_riding_hood.pdf"

PAGE_W, PAGE_H = 720.0, 960.0
MARGIN = 64.0

RED = (0.72, 0.14, 0.18)
RED_HI = (0.92, 0.28, 0.32)
CAPE = (0.78, 0.12, 0.16)
WOLF = (0.42, 0.40, 0.44)
WOLF_DK = (0.24, 0.22, 0.26)
FOREST = (0.14, 0.34, 0.22)
FOREST_LT = (0.28, 0.52, 0.34)
SKIN = (0.97, 0.82, 0.66)
INK = (0.13, 0.12, 0.16)
PARCHMENT = (0.97, 0.94, 0.88)
BASKET = (0.55, 0.38, 0.22)
WHITE = (0.96, 0.95, 0.93)

TITLE_FONT = "times-bold"
BODY_FONT = "times-roman"
ITALIC_FONT = "times-italic"


def _fill_background(page: fitz.Page, color: tuple[float, float, float]) -> None:
    page.draw_rect(page.rect, color=color, fill=color)


def _draw_tree(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    page.draw_rect(
        fitz.Rect(cx - 8 * scale, cy, cx + 8 * scale, cy + 70 * scale),
        color=(0.35, 0.22, 0.12),
        fill=(0.55, 0.36, 0.20),
    )
    page.draw_circle(fitz.Point(cx, cy - 10 * scale), 34 * scale, color=FOREST, fill=FOREST_LT, width=1.5)


def _draw_red_hood(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    page.draw_circle(
        fitz.Point(cx, cy - 28 * scale),
        14 * scale,
        color=(0.45, 0.30, 0.22),
        fill=SKIN,
        width=1.2,
    )
    hood = [
        fitz.Point(cx, cy - 44 * scale),
        fitz.Point(cx - 22 * scale, cy - 8 * scale),
        fitz.Point(cx + 22 * scale, cy - 8 * scale),
    ]
    page.draw_polyline(hood, color=CAPE, fill=RED, width=1.5, closePath=True)
    gown = [
        fitz.Point(cx, cy - 8 * scale),
        fitz.Point(cx - 34 * scale, cy + 62 * scale),
        fitz.Point(cx + 34 * scale, cy + 62 * scale),
    ]
    page.draw_polyline(gown, color=(0.35, 0.08, 0.10), fill=RED_HI, width=1.5, closePath=True)


def _draw_basket(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    body = fitz.Rect(cx - 24 * scale, cy - 16 * scale, cx + 24 * scale, cy + 16 * scale)
    page.draw_rect(body, color=(0.35, 0.22, 0.12), fill=BASKET, width=1.2)
    handle = fitz.Rect(cx - 24 * scale, cy - 34 * scale, cx + 24 * scale, cy - 4 * scale)
    page.draw_oval(handle, color=(0.35, 0.22, 0.12), width=2)


def _draw_wolf(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    body = fitz.Rect(cx - 52 * scale, cy - 18 * scale, cx + 52 * scale, cy + 28 * scale)
    page.draw_oval(body, color=WOLF_DK, fill=WOLF, width=1.5)
    head = fitz.Rect(cx + 30 * scale, cy - 34 * scale, cx + 78 * scale, cy + 8 * scale)
    page.draw_oval(head, color=WOLF_DK, fill=WOLF, width=1.5)
    page.draw_circle(fitz.Point(cx + 58 * scale, cy - 12 * scale), 4 * scale, fill=INK)
    page.draw_circle(fitz.Point(cx + 70 * scale, cy - 12 * scale), 4 * scale, fill=INK)
    page.draw_polyline(
        [
            fitz.Point(cx + 88 * scale, cy - 8 * scale),
            fitz.Point(cx + 96 * scale, cy - 2 * scale),
            fitz.Point(cx + 88 * scale, cy + 2 * scale),
        ],
        color=WOLF_DK,
        fill=WOLF_DK,
        closePath=True,
    )


def _draw_grandmother(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    page.draw_circle(
        fitz.Point(cx, cy - 24 * scale),
        16 * scale,
        color=(0.45, 0.30, 0.22),
        fill=SKIN,
        width=1.2,
    )
    page.draw_oval(
        fitz.Rect(cx - 28 * scale, cy - 38 * scale, cx + 28 * scale, cy - 10 * scale),
        color=(0.75, 0.72, 0.68),
        fill=WHITE,
        width=1.2,
    )
    shawl = [
        fitz.Point(cx, cy - 6 * scale),
        fitz.Point(cx - 40 * scale, cy + 58 * scale),
        fitz.Point(cx + 40 * scale, cy + 58 * scale),
    ]
    page.draw_polyline(shawl, color=(0.55, 0.12, 0.16), fill=RED, width=1.5, closePath=True)


def _draw_cottage(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    base = fitz.Rect(cx - 70 * scale, cy - 10 * scale, cx + 70 * scale, cy + 70 * scale)
    page.draw_rect(base, color=(0.35, 0.22, 0.12), fill=(0.82, 0.72, 0.58), width=1.5)
    roof = [
        fitz.Point(cx - 82 * scale, cy - 10 * scale),
        fitz.Point(cx, cy - 62 * scale),
        fitz.Point(cx + 82 * scale, cy - 10 * scale),
    ]
    page.draw_polyline(roof, color=(0.35, 0.12, 0.10), fill=RED, width=1.5, closePath=True)
    page.draw_rect(
        fitz.Rect(cx - 18 * scale, cy + 18 * scale, cx + 18 * scale, cy + 70 * scale),
        color=(0.35, 0.22, 0.12),
        fill=(0.45, 0.28, 0.18),
    )


def _cover_raster(width: int = 520, height: int = 300) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (214, 228, 206))
    d = ImageDraw.Draw(img)
    d.rectangle([0, int(height * 0.55), width, height], fill=(34, 86, 56))
    for x in (70, 180, 360, 450):
        d.rectangle([x - 5, int(height * 0.58), x + 5, int(height * 0.78)], fill=(90, 58, 30))
        d.ellipse([x - 28, int(height * 0.34), x + 28, int(height * 0.62)], fill=(46, 118, 72))
    hx, hy = int(width * 0.42), int(height * 0.62)
    d.polygon([(hx, hy - 34), (hx - 20, hy + 4), (hx + 20, hy + 4)], fill=(186, 36, 46))
    d.polygon([(hx, hy + 4), (hx - 30, hy + 58), (hx + 30, hy + 58)], fill=(214, 72, 82))
    bx, by = int(width * 0.58), int(height * 0.68)
    d.rectangle([bx - 18, by - 10, bx + 18, by + 10], fill=(140, 96, 56))
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
        color=(0.40, 0.36, 0.31),
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
        color=(0.40, 0.36, 0.31),
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
        "1. Into the woods",
        (
            "Once upon a time there was a sweet little girl whom everyone loved. "
            "Her grandmother gave her a little cap of red velvet, and because she "
            "would wear nothing else, she was called Little Red Riding Hood. One day "
            "her mother said, \"Take this cake and bottle of wine to your grandmother; "
            "she has been ill. Go quietly and do not leave the path.\" Red Riding Hood "
            "promised, took her basket, and set out through the wood."
        ),
        lambda page, panel: (
            _draw_tree(page, panel.x0 + 80, panel.y0 + 180, scale=1.0),
            _draw_tree(page, panel.x1 - 90, panel.y0 + 170, scale=1.1),
            _draw_red_hood(page, panel.x0 + 250, panel.y0 + 190, scale=1.1),
            _draw_basket(page, panel.x0 + 290, panel.y0 + 220, scale=0.9),
        ),
    )

    _story_page(
        doc,
        "2. The wolf on the path",
        (
            "The wolf had been watching the house. When Red Riding Hood entered the "
            "wood, he came up and asked where she was going. \"To my grandmother,\" "
            "she said, \"who lives beyond the mill under the three great oak-trees.\" "
            "The wolf thought, \"What a tender young creature — what a nice plump mouthful!\" "
            "But he did not dare eat her in daylight, so he walked beside her and asked "
            "about the flowers. Red Riding Hood looked around at the blossoms and stepped "
            "deeper into the wood."
        ),
        lambda page, panel: (
            _draw_tree(page, panel.x0 + 120, panel.y0 + 170, scale=0.9),
            _draw_red_hood(page, panel.x0 + 220, panel.y0 + 190, scale=1.0),
            _draw_wolf(page, panel.x1 - 180, panel.y0 + 200, scale=0.95),
        ),
    )

    _story_page(
        doc,
        "3. At grandmother's door",
        (
            "While Red Riding Hood gathered flowers, the wolf ran straight to the "
            "grandmother's house and knocked. \"Who is there?\" \"Little Red Riding Hood, "
            "with cake and wine.\" \"Lift the latch and come in,\" said the grandmother. "
            "The wolf sprang in, swallowed the old woman whole, put on her cap and "
            "nightgown, and lay down in the bed with the curtains drawn."
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 180, panel.y0 + 210, scale=1.0),
            _draw_wolf(page, panel.x0 + 360, panel.y0 + 210, scale=0.75),
        ),
    )

    _story_page(
        doc,
        "4. All is well again",
        (
            "When Red Riding Hood arrived she found the door open and the room strange. "
            "\"Grandmother, what big eyes you have!\" \"The better to see you with, my "
            "child.\" \"Grandmother, what big teeth you have!\" \"The better to eat you "
            "with!\" cried the wolf, and leaped from the bed. A huntsman heard the cry, "
            "rushed in, and rescued them both. The grandmother drank the wine, ate the "
            "cake, and Red Riding Hood promised never again to stray from the path."
        ),
        lambda page, panel: (
            _draw_grandmother(page, panel.x0 + 150, panel.y0 + 180, scale=1.0),
            _draw_red_hood(page, panel.x0 + 280, panel.y0 + 190, scale=0.95),
            _draw_basket(page, panel.x0 + 320, panel.y0 + 220, scale=0.85),
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
    assert total_images >= 1, "expected an embedded raster image"
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
    print("Built + verified demo book:")
    for key, value in info.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
