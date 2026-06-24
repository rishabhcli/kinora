#!/usr/bin/env python3
"""Build Kinora's second bundled public-domain demo book as a real PDF.

Story: **"Little Red Riding Hood"** — an abridged retelling of the Brothers
Grimm fairy tale (*Rotkäppchen*, Kinder- und Hausmärchen, 1812). The tale and
its 19th-century English translations are in the **public domain**.

The book mirrors the Frog-King demo format:

* a title page + four story pages (five total),
* three characters for the ingest canon — **Little Red Riding Hood**, the
  **Wolf**, and **Grandmother**,
* a selectable text layer on every page, and
* simple vector illustrations plus one embedded raster cover scene.

Run::

    backend/.venv/bin/python assets/books/build_little_red_riding_hood.py

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

# Forest-storybook palette.
RED = (0.72, 0.14, 0.18)
RED_DK = (0.48, 0.08, 0.12)
CAPE = (0.82, 0.12, 0.16)
WOLF = (0.42, 0.40, 0.38)
WOLF_DK = (0.24, 0.22, 0.20)
FOREST = (0.18, 0.38, 0.24)
FOREST_DK = (0.10, 0.24, 0.14)
SKY = (0.72, 0.86, 0.94)
PARCHMENT = (0.97, 0.95, 0.89)
INK = (0.13, 0.12, 0.16)
WOOD = (0.55, 0.36, 0.22)
WOOD_DK = (0.35, 0.22, 0.12)
GRAY = (0.72, 0.70, 0.68)

TITLE_FONT = "times-bold"
BODY_FONT = "times-roman"


def _fill_background(page: fitz.Page, color: tuple[float, float, float]) -> None:
    page.draw_rect(page.rect, color=color, fill=color)


def _draw_trees(page: fitz.Page, panel: fitz.Rect) -> None:
    for x in (panel.x0 + 50, panel.x0 + 150, panel.x1 - 120, panel.x1 - 40):
        page.draw_polyline(
            [
                fitz.Point(x, panel.y0 + 30),
                fitz.Point(x - 28, panel.y0 + 120),
                fitz.Point(x + 28, panel.y0 + 120),
            ],
            color=FOREST_DK,
            fill=FOREST,
            width=1.2,
            closePath=True,
        )
        page.draw_rect(
            fitz.Rect(x - 5, panel.y0 + 120, x + 5, panel.y0 + 150),
            color=WOOD_DK,
            fill=WOOD,
        )


def _draw_red_hood(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    """Little Red Riding Hood: cape triangle, face, basket."""
    page.draw_circle(
        fitz.Point(cx, cy - 30 * scale),
        14 * scale,
        color=(0.5, 0.36, 0.26),
        fill=(0.97, 0.82, 0.66),
        width=1.2,
    )
    cape = [
        fitz.Point(cx, cy - 18 * scale),
        fitz.Point(cx - 34 * scale, cy + 50 * scale),
        fitz.Point(cx + 34 * scale, cy + 50 * scale),
    ]
    page.draw_polyline(cape, color=RED_DK, fill=CAPE, width=1.5, closePath=True)
    page.draw_circle(
        fitz.Point(cx, cy - 42 * scale),
        18 * scale,
        color=RED_DK,
        fill=RED,
        width=1.2,
    )
    basket = fitz.Rect(cx + 20 * scale, cy + 10 * scale, cx + 44 * scale, cy + 34 * scale)
    page.draw_rect(basket, color=WOOD_DK, fill=WOOD, width=1.2)


def _draw_wolf(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    """A sly wolf: body, snout, ears, tail."""
    body = fitz.Rect(cx - 50 * scale, cy - 20 * scale, cx + 50 * scale, cy + 30 * scale)
    page.draw_oval(body, color=WOLF_DK, fill=WOLF, width=1.5)
    snout = fitz.Rect(cx + 30 * scale, cy - 10 * scale, cx + 70 * scale, cy + 16 * scale)
    page.draw_oval(snout, color=WOLF_DK, fill=WOLF, width=1.2)
    for dx in (-30 * scale, 10 * scale):
        ear = fitz.Rect(cx + dx - 10 * scale, cy - 48 * scale, cx + dx + 10 * scale, cy - 22 * scale)
        page.draw_oval(ear, color=WOLF_DK, fill=WOLF, width=1.2)
    page.draw_circle(fitz.Point(cx + 58 * scale, cy - 2 * scale), 4 * scale, color=None, fill=INK)
    page.draw_bezier(
        fitz.Point(cx + 40 * scale, cy + 8 * scale),
        fitz.Point(cx + 52 * scale, cy + 18 * scale),
        fitz.Point(cx + 64 * scale, cy + 18 * scale),
        fitz.Point(cx + 76 * scale, cy + 8 * scale),
        color=WOLF_DK,
        width=2,
    )
    page.draw_bezier(
        fitz.Point(cx - 48 * scale, cy + 10 * scale),
        fitz.Point(cx - 70 * scale, cy + 30 * scale),
        fitz.Point(cx - 82 * scale, cy + 20 * scale),
        fitz.Point(cx - 90 * scale, cy + 6 * scale),
        color=WOLF_DK,
        width=4 * scale,
    )


def _draw_cottage(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    """Grandmother's cottage with a chimney."""
    w = 120 * scale
    h = 80 * scale
    body = fitz.Rect(cx - w / 2, cy, cx + w / 2, cy + h)
    page.draw_rect(body, color=WOOD_DK, fill=WOOD, width=1.5)
    roof = [
        fitz.Point(cx - w / 2 - 10 * scale, cy),
        fitz.Point(cx, cy - 50 * scale),
        fitz.Point(cx + w / 2 + 10 * scale, cy),
    ]
    page.draw_polyline(roof, color=(0.35, 0.12, 0.10), fill=RED_DK, width=1.5, closePath=True)
    page.draw_rect(
        fitz.Rect(cx - 18 * scale, cy + 30 * scale, cx + 18 * scale, cy + h),
        color=WOOD_DK,
        fill=(0.18, 0.10, 0.06),
    )
    page.draw_rect(
        fitz.Rect(cx + 30 * scale, cy - 70 * scale, cx + 44 * scale, cy - 20 * scale),
        color=GRAY,
        fill=GRAY,
        width=1.2,
    )


def _draw_grandmother(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    """Grandmother in bed: cap, blanket, spectacles."""
    page.draw_rect(
        fitz.Rect(cx - 60 * scale, cy, cx + 60 * scale, cy + 40 * scale),
        color=(0.35, 0.20, 0.45),
        fill=(0.55, 0.32, 0.62),
        width=1.2,
    )
    page.draw_circle(
        fitz.Point(cx, cy - 18 * scale),
        16 * scale,
        color=(0.5, 0.36, 0.26),
        fill=(0.97, 0.86, 0.76),
        width=1.2,
    )
    page.draw_oval(
        fitz.Rect(cx - 22 * scale, cy - 38 * scale, cx + 22 * scale, cy - 10 * scale),
        color=(0.9, 0.9, 0.92),
        fill=(0.96, 0.96, 0.98),
        width=1.2,
    )
    for dx in (-12 * scale, 12 * scale):
        page.draw_circle(fitz.Point(cx + dx, cy - 20 * scale), 8 * scale, color=None, fill=None, width=1.5)


def _cover_raster(width: int = 520, height: int = 300) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (214, 232, 220))
    d = ImageDraw.Draw(img)
    d.rectangle([0, int(height * 0.55), width, height], fill=(96, 132, 78))
    for x in (50, 140, 360, 450):
        d.polygon([(x, 60), (x - 30, 170), (x + 30, 170)], fill=(46, 96, 58))
        d.rectangle([x - 5, 170, x + 5, 195], fill=(110, 72, 38))
    # Red hood figure.
    d.ellipse([width // 2 - 18, 108, width // 2 + 18, 144], fill=(196, 36, 48))
    d.polygon(
        [(width // 2, 140), (width // 2 - 36, 230), (width // 2 + 36, 230)],
        fill=(208, 32, 42),
    )
    d.ellipse([width // 2 - 12, 122, width // 2 + 12, 146], fill=(244, 206, 176))
    # Wolf in the trees.
    wx = int(width * 0.78)
    d.ellipse([wx - 40, 150, wx + 40, 200], fill=(108, 102, 98))
    d.ellipse([wx + 10, 158, wx + 46, 182], fill=(108, 102, 98))
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
        "Little Red\nRiding Hood",
        fontname=TITLE_FONT,
        fontsize=42,
        color=INK,
        align=fitz.TEXT_ALIGN_CENTER,
    )
    page.insert_textbox(
        fitz.Rect(MARGIN, 250, PAGE_W - MARGIN, 290),
        "A public-domain tale · Brothers Grimm",
        fontname=BODY_FONT,
        fontsize=14,
        color=(0.35, 0.32, 0.30),
        align=fitz.TEXT_ALIGN_CENTER,
    )
    panel = fitz.Rect(MARGIN, 320, PAGE_W - MARGIN, PAGE_H - MARGIN)
    page.draw_rect(panel, color=SKY, fill=SKY)
    _draw_trees(page, panel)
    _draw_red_hood(page, panel.x0 + panel.width / 2, panel.y0 + 150, scale=1.3)
    page.insert_image(panel, stream=_cover_raster(), keep_proportion=True, overlay=True)


def _story_page(
    doc: fitz.Document,
    heading: str,
    body: str,
    draw_scene,
) -> None:
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    _fill_background(page, PARCHMENT)
    _heading(page, heading, MARGIN)
    _paragraph(page, body, MARGIN + 52, height=260)
    panel = fitz.Rect(MARGIN, 390, PAGE_W - MARGIN, PAGE_H - MARGIN)
    page.draw_rect(panel, color=SKY, fill=SKY)
    draw_scene(page, panel)


def build() -> Path:
    doc = fitz.open()
    doc.set_metadata(
        {
            "title": "Little Red Riding Hood",
            "author": "Brothers Grimm (public domain)",
            "subject": "Kinora demo book — public domain fairy tale",
        }
    )

    _title_page(doc)

    _story_page(
        doc,
        "1. Into the woods",
        (
            "Once upon a time there was a sweet little girl whom everyone loved. "
            "Her grandmother had made her a little red riding hood, and because she "
            "wore it so often, she was called Little Red Riding Hood. One day her "
            "mother said, \u201cTake this cake and bottle of wine to your grandmother, "
            "who is ill and weak. Go straight through the wood and do not leave the path.\u201d "
            "Little Red Riding Hood promised to obey, put on her hood, and set out "
            "with her basket through the tall green trees."
        ),
        lambda page, panel: (
            _draw_trees(page, panel),
            _draw_red_hood(page, panel.x0 + panel.width / 2, panel.y0 + 120, scale=1.2),
        ),
    )

    _story_page(
        doc,
        "2. The wolf on the path",
        (
            "As she walked, a wolf came out of the forest. He wished to eat her, "
            "but dared not while woodcutters were near. So he spoke kindly: "
            "\u201cGood day, Little Red Riding Hood. Where are you going so early?\u201d "
            "She told him of her grandmother. The wolf thought, \u201cWhat a tender "
            "young creature — she will taste better than the old woman.\u201d He said, "
            "\u201cSee how the flowers bloom! Why not gather a nosegay for your grandmother?\u201d "
            "The child left the path and wandered among the blossoms while the wolf "
            "ran straight to the grandmother\u2019s house."
        ),
        lambda page, panel: (
            _draw_trees(page, panel),
            _draw_red_hood(page, panel.x0 + 120, panel.y0 + 130, scale=1.0),
            _draw_wolf(page, panel.x1 - 130, panel.y0 + 140, scale=0.9),
        ),
    )

    _story_page(
        doc,
        "3. At grandmother\u2019s door",
        (
            "The wolf knocked at the cottage door. When the grandmother opened it, "
            "he sprang upon her and swallowed her whole. Then he put on her nightcap, "
            "drew the curtains, and lay down in her bed. Presently Little Red Riding "
            "Hood arrived and wondered why the door stood open. She went to the bed "
            "and said, \u201cOh, Grandmother, what big ears you have!\u201d \u201cThe better to "
            "hear you with,\u201d said the wolf. \u201cWhat big eyes you have!\u201d \u201cThe better "
            "to see you with.\u201d \u201cWhat big teeth you have!\u201d \u201cThe better to eat you "
            "with!\u201d And with that the wolf leaped from the bed."
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + panel.width / 2, panel.y0 + 40, scale=1.1),
            _draw_grandmother(page, panel.x0 + panel.width / 2, panel.y0 + 120, scale=1.0),
            _draw_wolf(page, panel.x1 - 100, panel.y0 + 130, scale=0.75),
        ),
    )

    _story_page(
        doc,
        "4. The woodcutter\u2019s rescue",
        (
            "A passing woodcutter heard the cry and ran into the cottage with his axe. "
            "He struck the wolf so that he opened his mouth and let the child spring out. "
            "Then the woodcutter cut open the sleeping wolf, and the grandmother stepped "
            "out alive and unharmed. Little Red Riding Hood fetched great stones, which "
            "they put inside the wolf; when he woke he tried to flee, but the stones "
            "were so heavy that he fell down dead. The grandmother ate the cake and "
            "drank the wine, and Little Red Riding Hood thought, \u201cI will never again "
            "leave the path and run into the wood when my mother has forbidden it.\u201d"
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + panel.width / 2 - 40, panel.y0 + 30, scale=1.0),
            _draw_red_hood(page, panel.x0 + 130, panel.y0 + 140, scale=1.0),
            _draw_grandmother(page, panel.x1 - 130, panel.y0 + 130, scale=0.9),
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
    print("Built + verified demo book:")
    for key, value in info.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
