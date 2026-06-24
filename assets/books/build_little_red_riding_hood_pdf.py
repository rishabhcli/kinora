#!/usr/bin/env python3
"""Build Kinora's second bundled public-domain demo book as a real PDF.

Story: **"Little Red Riding Hood"** — an abridged retelling of the Brothers
Grimm fairy tale (*Rotkäppchen*, Kinder- und Hausmärchen, 1812). Public domain.

Run::

    backend/.venv/bin/python assets/books/build_little_red_riding_hood_pdf.py

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
RED_DK = (0.48, 0.08, 0.12)
GREEN = (0.22, 0.48, 0.28)
GREEN_DK = (0.14, 0.32, 0.18)
BROWN = (0.45, 0.32, 0.22)
BROWN_DK = (0.30, 0.20, 0.14)
INK = (0.13, 0.12, 0.16)
PARCHMENT = (0.97, 0.95, 0.89)
SKY = (0.72, 0.86, 0.95)
CAPE = (0.78, 0.16, 0.20)

TITLE_FONT = "times-bold"
BODY_FONT = "times-roman"
ITALIC_FONT = "times-italic"


def _fill_background(page: fitz.Page, color: tuple[float, float, float]) -> None:
    page.draw_rect(page.rect, color=color, fill=color)


def _draw_forest(page: fitz.Page, panel: fitz.Rect) -> None:
    page.draw_rect(panel, color=GREEN_DK, fill=(0.88, 0.93, 0.86))
    for x in (panel.x0 + 40, panel.x0 + 120, panel.x1 - 100, panel.x1 - 30):
        page.draw_rect(
            fitz.Rect(x - 8, panel.y0 + 80, x + 8, panel.y1 - 20),
            color=BROWN_DK,
            fill=BROWN,
            width=1.2,
        )
        page.draw_circle(fitz.Point(x, panel.y0 + 70), 36, color=GREEN_DK, fill=GREEN, width=1.2)


def _draw_cottage(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    w = 120 * scale
    body = fitz.Rect(cx - w / 2, cy - 20 * scale, cx + w / 2, cy + 70 * scale)
    page.draw_rect(body, color=BROWN_DK, fill=(0.82, 0.72, 0.58), width=1.5)
    roof = [
        fitz.Point(cx, cy - 70 * scale),
        fitz.Point(cx - w / 2 - 10 * scale, cy - 10 * scale),
        fitz.Point(cx + w / 2 + 10 * scale, cy - 10 * scale),
    ]
    page.draw_polyline(roof, color=RED_DK, fill=RED, width=1.5, closePath=True)
    page.draw_rect(
        fitz.Rect(cx - 18 * scale, cy + 20 * scale, cx + 18 * scale, cy + 70 * scale),
        color=BROWN_DK,
        fill=BROWN,
        width=1.2,
    )


def _draw_red_riding_hood(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    page.draw_circle(
        fitz.Point(cx, cy - 30 * scale),
        16 * scale,
        color=(0.5, 0.36, 0.26),
        fill=(0.97, 0.82, 0.66),
        width=1.2,
    )
    page.draw_polyline(
        [
            fitz.Point(cx, cy - 14 * scale),
            fitz.Point(cx - 30 * scale, cy + 50 * scale),
            fitz.Point(cx + 30 * scale, cy + 50 * scale),
        ],
        color=CAPE,
        fill=CAPE,
        width=1.5,
        closePath=True,
    )
    page.draw_circle(fitz.Point(cx, cy - 38 * scale), 20 * scale, color=RED_DK, fill=RED, width=1.2)


def _draw_wolf(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    body = fitz.Rect(cx - 50 * scale, cy - 20 * scale, cx + 50 * scale, cy + 30 * scale)
    page.draw_oval(body, color=(0.35, 0.35, 0.38), fill=(0.55, 0.55, 0.58), width=1.5)
    page.draw_circle(fitz.Point(cx + 42 * scale, cy - 10 * scale), 22 * scale, color=(0.35, 0.35, 0.38), fill=(0.55, 0.55, 0.58), width=1.2)
    for ex in (cx + 34 * scale, cx + 50 * scale):
        page.draw_circle(fitz.Point(ex, cy - 18 * scale), 4 * scale, color=None, fill=INK)
    page.draw_circle(fitz.Point(cx + 58 * scale, cy - 2 * scale), 5 * scale, color=None, fill=INK)


def _draw_basket(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    page.draw_rect(
        fitz.Rect(cx - 22 * scale, cy - 10 * scale, cx + 22 * scale, cy + 24 * scale),
        color=BROWN_DK,
        fill=(0.72, 0.55, 0.34),
        width=1.2,
    )
    page.draw_bezier(
        fitz.Point(cx - 18 * scale, cy - 10 * scale),
        fitz.Point(cx - 18 * scale, cy - 34 * scale),
        fitz.Point(cx + 18 * scale, cy - 34 * scale),
        fitz.Point(cx + 18 * scale, cy - 10 * scale),
        color=BROWN_DK,
        width=2,
    )


def _cover_raster(width: int = 520, height: int = 300) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (231, 224, 196))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, int(height * 0.55)], fill=(198, 220, 210))
    d.rectangle([0, int(height * 0.55), width, height], fill=(120, 150, 96))
    for x in (50, 140, width - 120):
        d.polygon([(x, 60), (x - 28, 170), (x + 28, 170)], fill=(40, 92, 60))
    hx, hy = int(width * 0.42), int(height * 0.58)
    d.ellipse([hx - 14, hy - 44, hx + 14, hy - 16], fill=(248, 210, 178))
    d.polygon([(hx, hy - 8), (hx - 26, hy + 48), (hx + 26, hy + 48)], fill=(198, 40, 52))
    d.ellipse([hx - 22, hy - 52, hx + 22, hy - 28], fill=(198, 40, 52))
    d.rectangle([hx + 30, hy + 8, hx + 54, hy + 30], fill=(184, 140, 80))
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
    )


def _paragraph(page: fitz.Page, text: str, y: float) -> None:
    page.insert_textbox(
        fitz.Rect(MARGIN, y, PAGE_W - MARGIN, y + 300),
        text,
        fontname=BODY_FONT,
        fontsize=15,
        color=INK,
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
        color=BROWN_DK,
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
        color=BROWN_DK,
        align=fitz.TEXT_ALIGN_CENTER,
    )


def _story_page(doc: fitz.Document, heading: str, body: str, illustrate) -> None:
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    _fill_background(page, PARCHMENT)
    _heading(page, heading, MARGIN)
    _paragraph(page, body, MARGIN + 56)
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
        }
    )
    _title_page(doc)
    _story_page(
        doc,
        "1. Through the woods",
        (
            "Once upon a time there was a sweet little girl whom everyone loved. "
            "Her grandmother gave her a little cap of red velvet, and because she "
            "wore it so often she was called Little Red Riding Hood. One day her "
            "mother said, \"Take this cake and bottle of wine to your grandmother; "
            "she is ill and weak, and they will do her good. Walk properly and do "
            "not leave the path, or you may fall and break the bottle.\" Little Red "
            "Riding Hood promised to obey, and set out through the wood."
        ),
        lambda page, panel: (
            _draw_forest(page, panel),
            _draw_red_riding_hood(page, panel.x0 + 160, panel.y0 + 200, scale=1.1),
            _draw_basket(page, panel.x0 + 200, panel.y0 + 250, scale=1.0),
        ),
    )
    _story_page(
        doc,
        "2. The wolf on the path",
        (
            "The wolf had been wishing for a good long while to eat her up, but he "
            "dared not because of some woodcutters near. When he met Little Red "
            "Riding Hood he asked where she was going. She told him, and the wolf "
            "thought, \"The tender young thing will make a dainty morsel.\" He walked "
            "beside her a while, then said, \"Look at the pretty flowers! Why do you "
            "not look round?\" She ran from the path into the wood to pick flowers, "
            "and the wolf ran straight to the grandmother's house."
        ),
        lambda page, panel: (
            _draw_forest(page, panel),
            _draw_red_riding_hood(page, panel.x0 + 120, panel.y0 + 210, scale=1.0),
            _draw_wolf(page, panel.x1 - 150, panel.y0 + 200, scale=1.0),
        ),
    )
    _story_page(
        doc,
        "3. At grandmother's door",
        (
            "The wolf knocked at the door. \"Who is there?\" called the grandmother. "
            "\"Little Red Riding Hood,\" answered the wolf, \"with cake and wine.\" "
            "The old woman called, \"Pull the bobbin, and the latch will go up.\" "
            "The wolf went in, swallowed the grandmother at one gulp, put on her "
            "nightcap, and lay down in her bed with the curtains drawn. When Little "
            "Red Riding Hood arrived she was surprised how strange her grandmother "
            "looked, and said, \"Grandmother, what big eyes you have!\" \"The better "
            "to see you with, my child,\" said the wolf."
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 120, panel.y0 + 230, scale=1.0),
            _draw_wolf(page, panel.x1 - 160, panel.y0 + 180, scale=0.9),
            _draw_red_riding_hood(page, panel.x1 - 90, panel.y0 + 250, scale=0.85),
        ),
    )
    _story_page(
        doc,
        "4. The woodcutters",
        (
            "\"Grandmother, what big teeth you have!\" cried Little Red Riding Hood. "
            "\"The better to eat you with!\" shouted the wolf, and sprang out of bed. "
            "She screamed, and the woodcutters, who were passing near the house, "
            "rushed in with their axes. The wolf ran away, but they followed and "
            "soon killed him. Then they opened the wolf's body and the grandmother "
            "stepped out, alive and well. Little Red Riding Hood fetched great stones, "
            "they filled the wolf's belly, and when he woke and tried to flee he fell "
            "dead. And Little Red Riding Hood went home happily, and no one did her "
            "any harm again."
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 260, panel.y0 + 230, scale=0.9),
            _draw_red_riding_hood(page, panel.x0 + 130, panel.y0 + 210, scale=1.0),
            _draw_wolf(page, panel.x0 + 220, panel.y0 + 250, scale=0.75),
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
    finally:
        doc.close()
    assert num_pages >= 4
    assert total_words >= 200
    assert total_drawings >= 1
    assert total_images >= 1
    return {
        "path": str(path),
        "pages": num_pages,
        "words": total_words,
        "size_kb": round(path.stat().st_size / 1024, 1),
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
