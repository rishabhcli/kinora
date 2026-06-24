#!/usr/bin/env python3
"""Build Kinora's second bundled public-domain demo book as a multi-page PDF.

Story: **"Little Red Riding Hood"** — an abridged retelling of the Charles Perrault /
Brothers Grimm fairy tale. The tale and its 19th-century English translations are in
the **public domain**.

Deliberately small and demo-friendly (title + four story pages):

* three lockable characters — **Little Red Riding Hood**, the **Wolf**, and
  **Grandmother**,
* a real text layer on every page,
* vector illustrations on each page plus one embedded raster cover.

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

RED = (0.72, 0.14, 0.18)
RED_DK = (0.48, 0.08, 0.12)
CAPE = (0.82, 0.12, 0.16)
WOLF = (0.42, 0.40, 0.38)
WOLF_DK = (0.28, 0.26, 0.24)
FOREST = (0.18, 0.38, 0.24)
FOREST_DK = (0.10, 0.24, 0.14)
SKIN = (0.96, 0.86, 0.76)
INK = (0.13, 0.12, 0.16)
PARCHMENT = (0.97, 0.95, 0.89)
COTTAGE = (0.78, 0.62, 0.42)
COTTAGE_DK = (0.55, 0.42, 0.28)
WHITE = (0.98, 0.98, 0.96)

TITLE_FONT = "times-bold"
BODY_FONT = "times-roman"
ITALIC_FONT = "times-italic"


def _fill_background(page: fitz.Page, color: tuple[float, float, float]) -> None:
    page.draw_rect(page.rect, color=color, fill=color)


def _draw_trees(page: fitz.Page, panel: fitz.Rect, count: int = 5) -> None:
    step = panel.width / (count + 1)
    for i in range(count):
        x = panel.x0 + step * (i + 1)
        page.draw_polyline(
            [fitz.Point(x, panel.y0 + 30), fitz.Point(x - 28, panel.y1 - 40), fitz.Point(x + 28, panel.y1 - 40)],
            color=FOREST_DK,
            fill=FOREST,
            closePath=True,
        )
        page.draw_rect(fitz.Rect(x - 5, panel.y1 - 40, x + 5, panel.y1 - 10), color=COTTAGE_DK, fill=COTTAGE_DK)


def _draw_red_riding_hood(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    s = scale
    page.draw_circle(fitz.Point(cx, cy - 40 * s), 18 * s, color=SKIN, fill=SKIN)
    page.draw_circle(fitz.Point(cx - 6 * s, cy - 44 * s), 3 * s, color=INK, fill=INK)
    page.draw_circle(fitz.Point(cx + 6 * s, cy - 44 * s), 3 * s, color=INK, fill=INK)
    page.draw_rect(
        fitz.Rect(cx - 22 * s, cy - 22 * s, cx + 22 * s, cy + 55 * s),
        color=CAPE,
        fill=RED,
    )
    page.draw_circle(fitz.Point(cx, cy - 52 * s), 20 * s, color=RED, fill=CAPE)
    page.draw_rect(fitz.Rect(cx - 14 * s, cy + 55 * s, cx - 4 * s, cy + 95 * s), color=INK, fill=INK)
    page.draw_rect(fitz.Rect(cx + 4 * s, cy + 55 * s, cx + 14 * s, cy + 95 * s), color=INK, fill=INK)


def _draw_triangle(page: fitz.Page, p1: fitz.Point, p2: fitz.Point, p3: fitz.Point, color, fill) -> None:
    page.draw_polyline([p1, p2, p3], color=color, fill=fill, closePath=True)


def _draw_wolf(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    s = scale
    body = fitz.Rect(cx - 55 * s, cy - 10 * s, cx + 55 * s, cy + 45 * s)
    page.draw_oval(body, color=WOLF_DK, fill=WOLF)
    head = fitz.Rect(cx + 35 * s, cy - 35 * s, cx + 85 * s, cy + 15 * s)
    page.draw_oval(head, color=WOLF_DK, fill=WOLF)
    page.draw_circle(fitz.Point(cx + 62 * s, cy - 8 * s), 4 * s, color=INK, fill=INK)
    _draw_triangle(
        page,
        fitz.Point(cx + 88 * s, cy - 5 * s),
        fitz.Point(cx + 98 * s, cy + 5 * s),
        fitz.Point(cx + 82 * s, cy + 8 * s),
        WOLF_DK,
        WOLF_DK,
    )
    for ex, ey in ((cx + 58 * s, cy - 28 * s), (cx + 72 * s, cy - 28 * s)):
        _draw_triangle(
            page,
            fitz.Point(ex, ey),
            fitz.Point(ex - 6 * s, ey - 18 * s),
            fitz.Point(ex + 6 * s, ey - 18 * s),
            WOLF,
            WOLF,
        )


def _draw_cottage(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    s = scale
    base = fitz.Rect(cx - 70 * s, cy, cx + 70 * s, cy + 70 * s)
    page.draw_rect(base, color=COTTAGE_DK, fill=COTTAGE)
    roof = fitz.Rect(cx - 85 * s, cy - 45 * s, cx + 85 * s, cy + 5 * s)
    page.draw_polyline(
        [fitz.Point(roof.x0, roof.y1), fitz.Point(cx, roof.y0), fitz.Point(roof.x1, roof.y1)],
        color=RED_DK,
        fill=RED,
        closePath=True,
    )
    page.draw_rect(fitz.Rect(cx - 18 * s, cy + 25 * s, cx + 18 * s, cy + 70 * s), color=INK, fill=INK)


def _draw_grandmother(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    s = scale
    page.draw_circle(fitz.Point(cx, cy - 35 * s), 20 * s, color=SKIN, fill=SKIN)
    page.draw_rect(fitz.Rect(cx - 24 * s, cy - 15 * s, cx + 24 * s, cy + 50 * s), color=WHITE, fill=WHITE)
    page.draw_circle(fitz.Point(cx, cy - 48 * s), 22 * s, color=WHITE, fill=WHITE)


def _cover_raster() -> bytes:
    from PIL import Image, ImageDraw

    width, height = 640, 420
    img = Image.new("RGB", (width, height), (34, 58, 38))
    d = ImageDraw.Draw(img)
    for x in (80, 200, 360, 500):
        d.polygon([(x, 60), (x - 40, 200), (x + 40, 200)], fill=(48, 110, 68))
        d.rectangle([x - 8, 200, x + 8, 240], fill=(70, 48, 28))
    # Red hood figure
    hx, hy = int(width * 0.38), int(height * 0.58)
    d.ellipse([hx - 22, hy - 70, hx + 22, hy - 30], fill=(210, 40, 48))
    d.rectangle([hx - 28, hy - 30, hx + 28, hy + 40], fill=(200, 32, 40))
    d.ellipse([hx - 16, hy - 88, hx + 16, hy - 56], fill=(230, 200, 175))
    # Basket
    bx = hx + 55
    d.rectangle([bx, hy, bx + 50, hy + 35], fill=(140, 96, 48), outline=(90, 60, 28))
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
        "An abridged public-domain fairy tale",
        fontname=ITALIC_FONT,
        fontsize=17,
        color=(0.4, 0.36, 0.31),
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
        color=(0.4, 0.36, 0.31),
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
            "author": "Public domain — abridged for Kinora",
            "subject": "Public-domain demo book for Kinora generation-on-scroll",
            "keywords": "public-domain, fairy tale, Kinora demo",
        }
    )

    _title_page(doc)

    _story_page(
        doc,
        "1. Through the woods",
        (
            "Once upon a time there was a sweet little girl whom everyone loved. Her "
            "grandmother loved her most of all, and made her a little red riding hood. "
            "One day her mother said, \"Take this cake and bottle of wine to your "
            "grandmother, who is ill. Go quietly and do not leave the path.\" Little "
            "Red Riding Hood promised to obey, put on her red hood, took her basket, "
            "and set out through the wood."
        ),
        lambda page, panel: (
            _draw_trees(page, panel),
            _draw_red_riding_hood(page, panel.x0 + 200, panel.y0 + 180, scale=1.1),
        ),
    )

    _story_page(
        doc,
        "2. The wolf on the path",
        (
            "She had not gone far when a wolf met her. He wished her good day, and asked "
            "whither she was going. The child told him she was taking cake and wine to "
            "her grandmother. The wolf thought, \"What a tender young creature — what "
            "a nice plump mouthful.\" But he walked beside her and said, \"Look at the "
            "pretty flowers. Why do you not look round?\" Little Red Riding Hood raised "
            "her eyes, and when she saw the sunbeams dancing through the trees, she left "
            "the path to pick a nosegay for her grandmother."
        ),
        lambda page, panel: (
            _draw_trees(page, panel, count=4),
            _draw_red_riding_hood(page, panel.x0 + 120, panel.y0 + 170, scale=1.0),
            _draw_wolf(page, panel.x1 - 160, panel.y0 + 200, scale=0.9),
        ),
    )

    _story_page(
        doc,
        "3. At grandmother's door",
        (
            "While Little Red Riding Hood gathered flowers, the wolf ran straight to the "
            "grandmother's house and knocked. \"Who is there?\" \"Little Red Riding Hood, "
            "with cake and wine.\" \"Lift the latch,\" said the grandmother. The wolf "
            "sprang in, swallowed the poor old woman, put on her nightcap, and lay down "
            "in her bed with the curtains drawn. When the child came to the cottage she "
            "was surprised to find the door unlatched, and everything seemed strange."
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 220, panel.y0 + 120, scale=1.2),
            _draw_grandmother(page, panel.x1 - 130, panel.y0 + 200, scale=0.8),
        ),
    )

    _story_page(
        doc,
        "4. \"What big eyes you have!\"",
        (
            "\"Good morning, Grandmother,\" she said, but received no answer. She drew "
            "the curtains, and there lay the grandmother with her cap pulled far over "
            "her face, looking very strange. \"Oh, Grandmother, what big ears you have!\" "
            "\"The better to hear you with.\" \"What big eyes you have!\" \"The better to "
            "see you with.\" \"What large hands you have!\" \"The better to hug you with.\" "
            "\"But, Grandmother, what a terrible big mouth you have!\" \"The better to eat "
            "you with!\" And scarcely had the wolf said this when he sprang out of bed and "
            "swallowed up Little Red Riding Hood."
        ),
        lambda page, panel: (
            _draw_grandmother(page, panel.x0 + 180, panel.y0 + 160, scale=1.1),
            _draw_wolf(page, panel.x1 - 170, panel.y0 + 220, scale=0.75),
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
    }


def main() -> int:
    path = build()
    info = verify(path)
    print("Built + verified Little Red Riding Hood demo book:")
    for key, value in info.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
