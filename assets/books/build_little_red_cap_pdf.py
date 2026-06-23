#!/usr/bin/env python3
"""Build Kinora's second bundled public-domain demo book as a real, multi-page PDF.

Story: **"Little Red Riding Hood"** — an abridged retelling of the Brothers Grimm
fairy tale (*Rotkäppchen*, Kinder- und Hausmärchen, 1812). The tale and its 19th
century English translations are firmly in the **public domain**.

Run with the backend venv::

    backend/.venv/bin/python assets/books/build_little_red_cap_pdf.py

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

RED = (0.78, 0.14, 0.18)
RED_DK = (0.52, 0.08, 0.12)
CAPE = (0.82, 0.12, 0.16)
WOLF = (0.42, 0.40, 0.44)
WOLF_DK = (0.24, 0.22, 0.26)
FOREST = (0.18, 0.40, 0.24)
FOREST_DK = (0.10, 0.24, 0.14)
WOOD = (0.55, 0.36, 0.20)
WOOD_DK = (0.35, 0.22, 0.12)
INK = (0.13, 0.12, 0.16)
PARCHMENT = (0.97, 0.95, 0.89)
SKIN = (0.97, 0.82, 0.66)
BASKET = (0.62, 0.42, 0.22)

TITLE_FONT = "times-bold"
BODY_FONT = "times-roman"
ITALIC_FONT = "times-italic"


def _fill_background(page: fitz.Page, color: tuple[float, float, float]) -> None:
    page.draw_rect(page.rect, color=color, fill=color)


def _draw_trees(page: fitz.Page, panel: fitz.Rect) -> None:
    for x in (panel.x0 + 70, panel.x0 + 200, panel.x1 - 90):
        page.draw_polyline(
            [fitz.Point(x, panel.y0 + 40), fitz.Point(x - 36, panel.y0 + 170), fitz.Point(x + 36, panel.y0 + 170)],
            color=FOREST_DK,
            fill=FOREST,
            width=1.2,
            closePath=True,
        )
        page.draw_rect(
            fitz.Rect(x - 7, panel.y0 + 170, x + 7, panel.y0 + 210),
            color=WOOD_DK,
            fill=WOOD,
        )


def _draw_red_riding_hood(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    gown = [
        fitz.Point(cx, cy - 18 * scale),
        fitz.Point(cx - 34 * scale, cy + 62 * scale),
        fitz.Point(cx + 34 * scale, cy + 62 * scale),
    ]
    page.draw_polyline(gown, color=RED_DK, fill=CAPE, width=1.4, closePath=True)
    page.draw_circle(
        fitz.Point(cx, cy - 32 * scale),
        15 * scale,
        color=(0.5, 0.36, 0.26),
        fill=SKIN,
        width=1.2,
    )
    hood = fitz.Rect(cx - 24 * scale, cy - 58 * scale, cx + 24 * scale, cy - 20 * scale)
    page.draw_oval(hood, color=RED_DK, fill=RED, width=1.4)


def _draw_basket(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    body = fitz.Rect(cx - 28 * scale, cy - 16 * scale, cx + 28 * scale, cy + 24 * scale)
    page.draw_rect(body, color=WOOD_DK, fill=BASKET, width=1.2)
    page.draw_bezier(
        fitz.Point(cx - 18 * scale, cy - 8 * scale),
        fitz.Point(cx - 18 * scale, cy - 38 * scale),
        fitz.Point(cx + 18 * scale, cy - 38 * scale),
        fitz.Point(cx + 18 * scale, cy - 8 * scale),
        color=WOOD_DK,
        width=2.5,
    )


def _draw_wolf(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    body = fitz.Rect(cx - 52 * scale, cy - 18 * scale, cx + 52 * scale, cy + 30 * scale)
    page.draw_oval(body, color=WOLF_DK, fill=WOLF, width=1.4)
    head = fitz.Rect(cx + 34 * scale, cy - 34 * scale, cx + 86 * scale, cy + 18 * scale)
    page.draw_oval(head, color=WOLF_DK, fill=WOLF, width=1.4)
    page.draw_circle(fitz.Point(cx + 62 * scale, cy - 8 * scale), 4 * scale, fill=INK)
    page.draw_polyline(
        [
            fitz.Point(cx + 86 * scale, cy - 2 * scale),
            fitz.Point(cx + 104 * scale, cy + 8 * scale),
            fitz.Point(cx + 86 * scale, cy + 14 * scale),
        ],
        color=WOLF_DK,
        fill=WOLF,
        closePath=True,
    )


def _draw_cottage(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    base = fitz.Rect(cx - 70 * scale, cy - 10 * scale, cx + 70 * scale, cy + 70 * scale)
    page.draw_rect(base, color=WOOD_DK, fill=WOOD, width=1.4)
    roof = [
        fitz.Point(cx - 86 * scale, cy - 10 * scale),
        fitz.Point(cx, cy - 70 * scale),
        fitz.Point(cx + 86 * scale, cy - 10 * scale),
    ]
    page.draw_polyline(roof, color=RED_DK, fill=RED, width=1.4, closePath=True)
    page.draw_rect(
        fitz.Rect(cx - 16 * scale, cy + 18 * scale, cx + 16 * scale, cy + 70 * scale),
        color=WOOD_DK,
        fill=(0.18, 0.12, 0.08),
    )


def _cover_raster(width: int = 520, height: int = 300) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (214, 228, 198))
    d = ImageDraw.Draw(img)
    d.rectangle([0, int(height * 0.35), width, height], fill=(72, 118, 72))
    for x in (70, 180, 360, 450):
        d.polygon([(x, 50), (x - 30, 170), (x + 30, 170)], fill=(34, 86, 52))
        d.rectangle([x - 5, 170, x + 5, 195], fill=(110, 72, 38))
    hx, hy = int(width * 0.42), int(height * 0.58)
    d.polygon([(hx, hy - 20), (hx - 28, hy + 55), (hx + 28, hy + 55)], fill=(196, 32, 42))
    d.ellipse([hx - 14, hy - 38, hx + 14, hy - 12], fill=(240, 198, 160))
    d.ellipse([hx - 22, hy - 58, hx + 22, hy - 22], fill=(200, 28, 38))
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

    title = doc.new_page(width=PAGE_W, height=PAGE_H)
    _fill_background(title, PARCHMENT)
    title.insert_textbox(
        fitz.Rect(MARGIN, 120, PAGE_W - MARGIN, 230),
        "Little Red Riding Hood",
        fontname=TITLE_FONT,
        fontsize=40,
        color=INK,
        align=fitz.TEXT_ALIGN_CENTER,
    )
    title.insert_textbox(
        fitz.Rect(MARGIN, 232, PAGE_W - MARGIN, 300),
        "An abridged retelling of the Brothers Grimm fairy tale",
        fontname=ITALIC_FONT,
        fontsize=17,
        color=(0.4, 0.36, 0.31),
        align=fitz.TEXT_ALIGN_CENTER,
    )
    title.insert_image(
        fitz.Rect(MARGIN + 30, 330, PAGE_W - MARGIN - 30, 640),
        stream=_cover_raster(),
        keep_proportion=True,
    )
    title.insert_textbox(
        fitz.Rect(MARGIN, 720, PAGE_W - MARGIN, 800),
        "Public domain · a Kinora demo book — watch the book.",
        fontname=BODY_FONT,
        fontsize=13,
        color=(0.4, 0.36, 0.31),
        align=fitz.TEXT_ALIGN_CENTER,
    )

    _story_page(
        doc,
        "1. Into the woods",
        (
            "Once upon a time there was a sweet little maiden. Everyone who saw her "
            "loved her, but her grandmother loved her most of all. One day her mother "
            "said, \"Take this cake and bottle of wine to your grandmother, who is ill "
            "and weak. Go straight there, and do not wander from the path into the "
            "wood, for the trees are thick and the way is long.\" The child promised, "
            "put on her little red cap, took the basket, and set out through the forest."
        ),
        lambda page, panel: (
            _draw_trees(page, panel),
            _draw_red_riding_hood(page, panel.x0 + 250, panel.y0 + 200, scale=1.2),
            _draw_basket(page, panel.x0 + 310, panel.y0 + 230, scale=1.0),
        ),
    )

    _story_page(
        doc,
        "2. The wolf on the path",
        (
            "As Little Red Riding Hood walked, a wolf met her. He wished to eat her, "
            "but dared not because of woodcutters nearby. \"Good day, Little Red "
            "Riding Hood,\" he said. \"Where are you going so early?\" \"To my "
            "grandmother, who lives beyond the mill under three great oaks.\" The wolf "
            "thought, \"The tender young thing will make a delicate morsel.\" He walked "
            "beside her a while, then said, \"See what pretty flowers grow here — why "
            "not pick a nosegay for your grandmother?\""
        ),
        lambda page, panel: (
            _draw_trees(page, panel),
            _draw_red_riding_hood(page, panel.x0 + 150, panel.y0 + 210, scale=1.1),
            _draw_wolf(page, panel.x1 - 180, panel.y0 + 210, scale=0.95),
        ),
    )

    _story_page(
        doc,
        "3. At grandmother's door",
        (
            "While the child gathered flowers, the wolf ran straight to the grandmother's "
            "house and knocked. \"Little Red Riding Hood,\" he called, \"with cake and "
            "wine — open the door.\" The door was unlatched, and the wolf sprang inside. "
            "When Little Red Riding Hood at last came to the cottage, she wondered that "
            "the door stood open. Everything looked so strange. She went to the bed and "
            "drew back the curtain. There lay her grandmother, but the cap was pulled far "
            "over her face, and she looked very odd."
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 220, panel.y0 + 180, scale=1.1),
            _draw_red_riding_hood(page, panel.x1 - 150, panel.y0 + 220, scale=0.95),
        ),
    )

    _story_page(
        doc,
        "4. The woodcutter's rescue",
        (
            "\"Oh, Grandmother, what big ears you have!\" \"The better to hear you with.\" "
            "\"What big eyes you have!\" \"The better to see you with.\" \"What big hands "
            "you have!\" \"The better to seize you with!\" \"What a terrible big mouth "
            "you have!\" \"The better to eat you with!\" The wolf sprang from the bed, "
            "but a huntsman passing by heard the cry, ran in, and rescued them. The "
            "grandmother drank the wine, ate the cake, and soon felt strong again. Little "
            "Red Riding Hood thought, \"Never again will I leave the path and run into "
            "the wood when my mother has forbidden it.\""
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 200, panel.y0 + 170, scale=1.0),
            _draw_wolf(page, panel.x0 + 280, panel.y0 + 230, scale=0.8),
            _draw_red_riding_hood(page, panel.x1 - 160, panel.y0 + 220, scale=1.0),
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
