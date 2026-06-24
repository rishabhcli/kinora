#!/usr/bin/env python3
"""Build a second bundled public-domain demo book as a real, multi-page PDF.

Story: **"Little Red Riding Hood"** — an abridged retelling of the Brothers
Grimm fairy tale (*Rotkäppchen*, Kinder- und Hausmärchen, 1812). The tale and
its 19th-century English translations are firmly in the **public domain**.

The book mirrors the Frog-King demo format:

* a title page + four story pages (five total),
* three characters the ingest canon will lock — **Little Red Riding Hood**,
  the **Wolf**, and **Grandmother**,
* a real, selectable text layer on every page, and
* simple vector illustrations on every page plus an embedded raster cover scene.

Run::

    backend/.venv/bin/python assets/books/build_little_red_riding_hood_pdf.py

It writes ``assets/books/little_red_riding_hood.pdf`` next to this script.
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
CAPE = (0.78, 0.12, 0.16)
WOLF = (0.42, 0.40, 0.38)
WOLF_DK = (0.24, 0.22, 0.20)
GREEN = (0.22, 0.48, 0.28)
GREEN_DK = (0.14, 0.32, 0.18)
WOOD = (0.55, 0.38, 0.22)
WOOD_DK = (0.35, 0.24, 0.14)
INK = (0.13, 0.12, 0.16)
PARCHMENT = (0.97, 0.95, 0.89)
SKIN = (0.97, 0.82, 0.66)
WHITE = (0.98, 0.96, 0.92)

TITLE_FONT = "times-bold"
BODY_FONT = "times-roman"
ITALIC_FONT = "times-italic"


def _fill_background(page: fitz.Page, color: tuple[float, float, float]) -> None:
    page.draw_rect(page.rect, color=color, fill=color)


def _draw_tree(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    page.draw_rect(
        fitz.Rect(cx - 8 * scale, cy, cx + 8 * scale, cy + 70 * scale),
        color=WOOD_DK,
        fill=WOOD,
        width=1.2,
    )
    page.draw_circle(fitz.Point(cx, cy - 10 * scale), 38 * scale, color=GREEN_DK, fill=GREEN, width=1.5)


def _draw_red_hood(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    page.draw_circle(fitz.Point(cx, cy - 20 * scale), 18 * scale, color=(0.5, 0.36, 0.26), fill=SKIN, width=1.2)
    hood = [
        fitz.Point(cx, cy - 38 * scale),
        fitz.Point(cx - 34 * scale, cy - 8 * scale),
        fitz.Point(cx + 34 * scale, cy - 8 * scale),
    ]
    page.draw_polyline(hood, color=RED_DK, fill=CAPE, width=1.5, closePath=True)
    gown = [
        fitz.Point(cx, cy - 4 * scale),
        fitz.Point(cx - 36 * scale, cy + 72 * scale),
        fitz.Point(cx + 36 * scale, cy + 72 * scale),
    ]
    page.draw_polyline(gown, color=RED_DK, fill=RED, width=1.5, closePath=True)
    page.draw_rect(
        fitz.Rect(cx - 14 * scale, cy + 30 * scale, cx + 14 * scale, cy + 52 * scale),
        color=WOOD_DK,
        fill=WOOD,
        width=1.0,
    )


def _draw_wolf(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    body = fitz.Rect(cx - 52 * scale, cy - 20 * scale, cx + 52 * scale, cy + 36 * scale)
    page.draw_oval(body, color=WOLF_DK, fill=WOLF, width=1.5)
    head = fitz.Rect(cx + 30 * scale, cy - 44 * scale, cx + 86 * scale, cy + 8 * scale)
    page.draw_oval(head, color=WOLF_DK, fill=WOLF, width=1.5)
    page.draw_circle(fitz.Point(cx + 58 * scale, cy - 18 * scale), 5 * scale, color=None, fill=INK)
    page.draw_circle(fitz.Point(cx + 74 * scale, cy - 18 * scale), 5 * scale, color=None, fill=INK)
    for dx in (-30 * scale, 10 * scale):
        page.draw_line(
            fitz.Point(cx + dx, cy + 30 * scale),
            fitz.Point(cx + dx * 1.2, cy + 52 * scale),
            color=WOLF_DK,
            width=4 * scale,
        )


def _draw_cottage(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    base = fitz.Rect(cx - 70 * scale, cy - 10 * scale, cx + 70 * scale, cy + 70 * scale)
    page.draw_rect(base, color=WOOD_DK, fill=(0.86, 0.78, 0.66), width=1.5)
    roof = [
        fitz.Point(cx, cy - 70 * scale),
        fitz.Point(cx - 88 * scale, cy - 8 * scale),
        fitz.Point(cx + 88 * scale, cy - 8 * scale),
    ]
    page.draw_polyline(roof, color=RED_DK, fill=RED, width=1.5, closePath=True)
    page.draw_rect(
        fitz.Rect(cx - 18 * scale, cy + 18 * scale, cx + 18 * scale, cy + 70 * scale),
        color=WOOD_DK,
        fill=WOOD,
        width=1.2,
    )


def _draw_grandmother(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    page.draw_circle(fitz.Point(cx, cy - 24 * scale), 20 * scale, color=(0.5, 0.36, 0.26), fill=SKIN, width=1.2)
    page.draw_circle(fitz.Point(cx, cy - 30 * scale), 24 * scale, color=WHITE, fill=WHITE, width=1.0)
    shawl = [
        fitz.Point(cx, cy - 8 * scale),
        fitz.Point(cx - 42 * scale, cy + 68 * scale),
        fitz.Point(cx + 42 * scale, cy + 68 * scale),
    ]
    page.draw_polyline(shawl, color=(0.5, 0.1, 0.14), fill=(0.68, 0.14, 0.18), width=1.5, closePath=True)


def _cover_raster(width: int = 520, height: int = 300) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (214, 228, 206))
    d = ImageDraw.Draw(img)
    d.rectangle([0, int(height * 0.55), width, height], fill=(88, 118, 72))
    for x in (70, 180, 360, 450):
        d.rectangle([x - 6, int(height * 0.58), x + 6, int(height * 0.82)], fill=(110, 74, 42))
        d.ellipse([x - 34, int(height * 0.28), x + 34, int(height * 0.62)], fill=(56, 122, 72))
    hx, hy = int(width * 0.38), int(height * 0.62)
    d.polygon([(hx, hy - 50), (hx - 30, hy - 10), (hx + 30, hy - 10)], fill=(198, 30, 40))
    d.rectangle([hx - 22, hy - 8, hx + 22, hy + 44], fill=(184, 36, 46))
    wx, wy = int(width * 0.68), int(height * 0.66)
    d.ellipse([wx - 40, wy - 18, wx + 40, wy + 22], fill=(108, 102, 98))
    d.ellipse([wx + 8, wy - 34, wx + 52, wy + 2], fill=(108, 102, 98))
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
    page.insert_image(fitz.Rect(MARGIN + 30, 330, PAGE_W - MARGIN - 30, 640), stream=_cover_raster(), keep_proportion=True)
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
        "1. Through the wood",
        (
            "Once upon a time there was a sweet little girl whom everyone loved. "
            "Her grandmother had given her a little red riding hood, and because she "
            "wore it so often, she was called Little Red Riding Hood. One day her mother "
            "said, \"Take this cake and bottle of wine to your grandmother; she is ill "
            "and weak, and they will do her good. Set out before it grows hot, and walk "
            "properly and do not run off the path, or you may fall and break the bottle. "
            "When you enter her house, give her a good-morning, and do not peep into "
            "every corner before you do it.\""
        ),
        lambda page, panel: (
            _draw_tree(page, panel.x0 + 80, panel.y0 + 180, scale=1.0),
            _draw_tree(page, panel.x1 - 90, panel.y0 + 200, scale=1.1),
            _draw_red_hood(page, panel.x0 + 220, panel.y0 + 170, scale=1.1),
        ),
    )

    _story_page(
        doc,
        "2. The wolf on the path",
        (
            "Little Red Riding Hood promised to obey, and set out. When she had gone a "
            "little way into the wood, a wolf met her. He wished her good-day, and asked "
            "where she was going. \"To my grandmother's,\" she said. \"She lives in the "
            "little house beyond the mill, under the three great oak-trees.\" The wolf "
            "thought, \"What a tender young creature — what a nice plump mouthful!\" But "
            "he did not dare eat her, for there were woodcutters near. So he walked beside "
            "her and asked whether she had seen the pretty flowers along the path."
        ),
        lambda page, panel: (
            _draw_tree(page, panel.x0 + 100, panel.y0 + 170, scale=0.9),
            _draw_red_hood(page, panel.x0 + 200, panel.y0 + 180, scale=1.0),
            _draw_wolf(page, panel.x1 - 150, panel.y0 + 190, scale=0.9),
        ),
    )

    _story_page(
        doc,
        "3. At grandmother's door",
        (
            "Little Red Riding Hood ran from the path into the wood to look for flowers, "
            "and each time she picked one she saw a still prettier one farther on. Meanwhile "
            "the wolf ran straight to the grandmother's house and knocked. \"Little Red "
            "Riding Hood,\" he called, \"with cake and wine. Open the door.\" The grandmother "
            "lived alone, and when she heard the voice she thought it must be the child and "
            "called, \"Pull the bobbin, and the latch will go up.\" The wolf did so, sprang "
            "in, and swallowed the grandmother whole."
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 250, panel.y0 + 210, scale=1.1),
            _draw_wolf(page, panel.x0 + 250, panel.y0 + 150, scale=0.75),
        ),
    )

    _story_page(
        doc,
        "4. All ends well",
        (
            "When Little Red Riding Hood reached the cottage, she was surprised to find the "
            "door standing open. She went to the bed and drew back the curtains. \"Oh, "
            "Grandmother, what big ears you have!\" \"All the better to hear you with.\" "
            "\"What big eyes you have!\" \"All the better to see you with.\" \"What big "
            "teeth you have!\" \"All the better to eat you with!\" At that moment a hunter "
            "passed by, heard the noise, and stepped inside. He cut open the wolf, and "
            "Grandmother sprang out. Little Red Riding Hood fetched great stones, they filled "
            "the wolf's body, and when he woke he could not run away. All three were happy."
        ),
        lambda page, panel: (
            _draw_grandmother(page, panel.x0 + 130, panel.y0 + 160, scale=1.0),
            _draw_red_hood(page, panel.x0 + 270, panel.y0 + 170, scale=0.95),
            _draw_cottage(page, panel.x1 - 120, panel.y0 + 220, scale=0.8),
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
