#!/usr/bin/env python3
"""Build Kinora's second bundled public-domain demo book as a real PDF.

Story: **"Little Red Riding Hood"** — an abridged retelling of the Brothers Grimm
fairy tale (*Rotkäppchen*, Kinder- und Hausmärchen, 1812). Public domain.

Deliberately small and demo-friendly: title page + four story pages, three
characters (Red Riding Hood, the Wolf, Grandmother), selectable text on every
page, and simple vector illustrations.

Run with the backend venv::

    backend/.venv/bin/python assets/books/build_little_red_riding_hood_pdf.py

Writes ``assets/books/little_red_riding_hood.pdf`` next to this script.
"""

from __future__ import annotations

import io
from pathlib import Path

import fitz  # PyMuPDF

HERE = Path(__file__).resolve().parent
OUT_PATH = HERE / "little_red_riding_hood.pdf"

PAGE_W, PAGE_H = 720.0, 960.0
MARGIN = 64.0

CAPE = (0.72, 0.12, 0.14)
CAPE_HI = (0.92, 0.28, 0.30)
CAPE_DK = (0.45, 0.08, 0.12)
WOLF = (0.38, 0.36, 0.34)
WOLF_DK = (0.22, 0.20, 0.18)
FOREST = (0.18, 0.38, 0.24)
FOREST_DK = (0.10, 0.24, 0.14)
SKY = (0.72, 0.84, 0.92)
PARCHMENT = (0.97, 0.95, 0.89)
INK = (0.13, 0.12, 0.16)
WOOD = (0.45, 0.30, 0.18)
BASKET = (0.62, 0.42, 0.22)

TITLE_FONT = "times-bold"
BODY_FONT = "times-roman"
ITALIC_FONT = "times-italic"


def _fill_background(page: fitz.Page, color: tuple[float, float, float]) -> None:
    page.draw_rect(page.rect, color=color, fill=color)


def _draw_trees(page: fitz.Page, panel: fitz.Rect) -> None:
    for x in (panel.x0 + 50, panel.x0 + 140, panel.x1 - 120, panel.x1 - 40):
        page.draw_polyline(
            [
                fitz.Point(x, panel.y0 + 40),
                fitz.Point(x - 28, panel.y0 + 150),
                fitz.Point(x + 28, panel.y0 + 150),
            ],
            color=FOREST_DK,
            fill=FOREST,
            width=1.2,
            closePath=True,
        )
        page.draw_rect(
            fitz.Rect(x - 5, panel.y0 + 150, x + 5, panel.y0 + 175),
            color=WOOD,
            fill=WOOD,
        )


def _draw_red_riding_hood(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    page.draw_circle(
        fitz.Point(cx, cy - 34 * scale),
        16 * scale,
        color=(0.5, 0.36, 0.26),
        fill=(0.97, 0.82, 0.66),
        width=1.2,
    )
    cape = [
        fitz.Point(cx, cy - 20 * scale),
        fitz.Point(cx - 42 * scale, cy + 70 * scale),
        fitz.Point(cx + 42 * scale, cy + 70 * scale),
    ]
    page.draw_polyline(cape, color=(0.45, 0.08, 0.12), fill=CAPE, width=1.5, closePath=True)
    page.draw_circle(fitz.Point(cx, cy - 52 * scale), 18 * scale, color=CAPE_DK, fill=CAPE_HI, width=1.2)


def _draw_wolf(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    body = fitz.Rect(cx - 55 * scale, cy - 20 * scale, cx + 55 * scale, cy + 40 * scale)
    page.draw_oval(body, color=WOLF_DK, fill=WOLF, width=1.5)
    head = fitz.Rect(cx + 30 * scale, cy - 50 * scale, cx + 90 * scale, cy + 10 * scale)
    page.draw_oval(head, color=WOLF_DK, fill=WOLF, width=1.5)
    page.draw_circle(fitz.Point(cx + 68 * scale, cy - 22 * scale), 5 * scale, fill=INK)
    page.draw_circle(fitz.Point(cx + 82 * scale, cy - 18 * scale), 4 * scale, fill=INK)
    for dx in (-30 * scale, 10 * scale):
        page.draw_line(
            fitz.Point(cx + dx, cy + 36 * scale),
            fitz.Point(cx + dx * 1.1, cy + 58 * scale),
            color=WOLF_DK,
            width=4 * scale,
        )


def _draw_cottage(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    w = 120 * scale
    h = 80 * scale
    base = fitz.Rect(cx - w / 2, cy, cx + w / 2, cy + h)
    page.draw_rect(base, color=WOOD, fill=(0.78, 0.62, 0.42), width=1.5)
    roof = [
        fitz.Point(cx - w / 2 - 10 * scale, cy),
        fitz.Point(cx, cy - 55 * scale),
        fitz.Point(cx + w / 2 + 10 * scale, cy),
    ]
    page.draw_polyline(roof, color=(0.35, 0.18, 0.10), fill=(0.55, 0.22, 0.16), width=1.5, closePath=True)
    page.draw_rect(
        fitz.Rect(cx - 18 * scale, cy + 28 * scale, cx + 18 * scale, cy + h),
        color=WOOD,
        fill=(0.35, 0.20, 0.10),
    )


def _draw_basket(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    body = fitz.Rect(cx - 24 * scale, cy - 10 * scale, cx + 24 * scale, cy + 22 * scale)
    page.draw_rect(body, color=(0.35, 0.22, 0.10), fill=BASKET, width=1.2)
    page.draw_polyline(
        [
            fitz.Point(cx - 18 * scale, cy - 2 * scale),
            fitz.Point(cx - 18 * scale, cy - 28 * scale),
            fitz.Point(cx + 18 * scale, cy - 28 * scale),
            fitz.Point(cx + 18 * scale, cy - 2 * scale),
        ],
        color=(0.35, 0.22, 0.10),
        width=2,
        closePath=False,
    )


def _cover_raster(width: int = 520, height: int = 300) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (231, 224, 196))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, int(height * 0.55)], fill=(184, 214, 232))
    d.rectangle([0, int(height * 0.55), width, height], fill=(96, 140, 88))
    for x in (50, 180, 360, 470):
        d.polygon([(x, 60), (x - 30, 180), (x + 30, 180)], fill=(46, 96, 58))
        d.rectangle([x - 5, 180, x + 5, 200], fill=(102, 68, 36))
    # Red cloak figure.
    d.ellipse([width // 2 - 18, 108, width // 2 + 18, 144], fill=(235, 196, 170))
    d.polygon(
        [(width // 2, 130), (width // 2 - 36, 220), (width // 2 + 36, 220)],
        fill=(184, 30, 36),
    )
    d.ellipse([width // 2 - 22, 88, width // 2 + 22, 118], fill=(220, 50, 58))
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
    img_rect = fitz.Rect(MARGIN + 30, 330, PAGE_W - MARGIN - 30, 640)
    page.insert_image(img_rect, stream=_cover_raster(), keep_proportion=True)
    page.insert_textbox(
        fitz.Rect(MARGIN, 720, PAGE_W - MARGIN, 800),
        "Public domain \u00b7 a Kinora demo book \u2014 watch the book.",
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
            "author": "Brothers Grimm (public domain) \u2014 abridged for Kinora",
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
            "Her grandmother had made her a little red velvet cap, and she wore it "
            "so often that she was called Little Red Riding Hood. One day her mother "
            "said, \u201cTake this cake and bottle of wine to your grandmother, who is ill. "
            "Go quietly and do not leave the path, for the woods are deep.\u201d Red Riding "
            "Hood promised, took her basket, and set out through the tall green forest."
        ),
        lambda page, panel: (
            _draw_trees(page, panel),
            _draw_red_riding_hood(page, panel.x0 + 200, panel.y0 + 210, scale=1.1),
            _draw_basket(page, panel.x0 + 250, panel.y0 + 250, scale=1.0),
        ),
    )

    _story_page(
        doc,
        "2. The wolf on the path",
        (
            "On the way she met a Wolf, who wished her good morning but thought to "
            "himself, \u201cWhat a tender young creature!\u201d He asked where she was going. "
            "\u201cTo my grandmother, who lives beyond the mill under the three great oaks.\u201d "
            "The Wolf ran ahead by the shorter path, knocked at the cottage door, and "
            "when the old woman opened it he swallowed her whole. Then he put on her "
            "nightcap, lay in her bed, and drew the curtains."
        ),
        lambda page, panel: (
            _draw_trees(page, panel),
            _draw_wolf(page, panel.x0 + 180, panel.y0 + 200, scale=1.0),
            _draw_cottage(page, panel.x1 - 150, panel.y0 + 230, scale=0.9),
        ),
    )

    _story_page(
        doc,
        "3. Grandmother, what big eyes!",
        (
            "When Red Riding Hood arrived she wondered that the door stood open. "
            "She went to the bed and said, \u201cGrandmother, what big ears you have!\u201d "
            "\u201cThe better to hear you with.\u201d \u201cGrandmother, what big eyes you have!\u201d "
            "\u201cThe better to see you with.\u201d \u201cGrandmother, what big teeth you have!\u201d "
            "\u201cThe better to eat you with!\u201d And the Wolf sprang from the bed. Red Riding "
            "Hood screamed so loudly that a huntsman, passing near, ran in and rescued her."
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 260, panel.y0 + 120, scale=1.1),
            _draw_wolf(page, panel.x0 + 250, panel.y0 + 250, scale=0.85),
            _draw_red_riding_hood(page, panel.x0 + 120, panel.y0 + 260, scale=0.95),
        ),
    )

    _story_page(
        doc,
        "4. Safe at home",
        (
            "The huntsman cut open the sleeping Wolf, and Grandmother stepped out, "
            "alive and unhurt. They filled the Wolf with heavy stones; when he woke "
            "and tried to flee, he sank. Red Riding Hood brought her grandmother the "
            "cake and wine, and the old woman soon felt stronger. Red Riding Hood "
            "thought, \u201cNever again will I wander off the path when my mother has "
            "warned me,\u201d and she went home through the woods as the sun set gold "
            "between the trees."
        ),
        lambda page, panel: (
            _draw_trees(page, panel),
            _draw_red_riding_hood(page, panel.x0 + 170, panel.y0 + 220, scale=1.0),
            _draw_basket(page, panel.x0 + 220, panel.y0 + 260, scale=0.9),
            _draw_cottage(page, panel.x1 - 140, panel.y0 + 200, scale=0.8),
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
    assert total_drawings >= 1
    assert total_images >= 1
    size_bytes = path.stat().st_size
    assert size_bytes < 5 * 1024 * 1024

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
