#!/usr/bin/env python3
"""Build Kinora's second bundled public-domain demo book as a multi-page PDF.

Story: **"Little Red Riding Hood"** — an abridged retelling of the Brothers
Grimm fairy tale (*Rotkäppchen*, Kinder- und Hausmärchen, 1812). The tale and
its 19th-century English translations are in the **public domain**.

The book mirrors the Frog-King demo: a title page + four story pages, three
principal characters (**Little Red Riding Hood**, the **Wolf**, and
**Grandmother**), selectable text on every page, and simple vector illustrations.

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

RED = (0.72, 0.14, 0.18)
RED_DK = (0.48, 0.08, 0.12)
CAPE = (0.78, 0.12, 0.16)
WOLF = (0.42, 0.40, 0.38)
WOLF_DK = (0.24, 0.22, 0.20)
FOREST = (0.18, 0.38, 0.24)
FOREST_DK = (0.10, 0.24, 0.14)
INK = (0.13, 0.12, 0.16)
PARCHMENT = (0.97, 0.95, 0.89)
SKIN = (0.97, 0.82, 0.66)
HAIR = (0.55, 0.36, 0.22)
BASKET = (0.55, 0.38, 0.22)
WHITE = (0.96, 0.95, 0.92)

TITLE_FONT = "times-bold"
BODY_FONT = "times-roman"
ITALIC_FONT = "times-italic"


def _fill_background(page: fitz.Page, color: tuple[float, float, float]) -> None:
    page.draw_rect(page.rect, color=color, fill=color)


def _draw_trees(page: fitz.Page, panel: fitz.Rect) -> None:
    for x in (panel.x0 + 40, panel.x0 + 120, panel.x1 - 80, panel.x1 - 30):
        page.draw_polyline(
            [fitz.Point(x, panel.y0 + 30), fitz.Point(x - 28, panel.y0 + 120), fitz.Point(x + 28, panel.y0 + 120)],
            color=FOREST_DK,
            fill=FOREST,
            width=1.2,
            closePath=True,
        )
        page.draw_rect(
            fitz.Rect(x - 5, panel.y0 + 120, x + 5, panel.y0 + 140),
            color=(0.35, 0.22, 0.12),
            fill=(0.55, 0.36, 0.20),
        )


def _draw_red_riding_hood(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    page.draw_circle(
        fitz.Point(cx, cy - 34 * scale),
        16 * scale,
        color=(0.45, 0.30, 0.20),
        fill=SKIN,
        width=1.2,
    )
    cape = [
        fitz.Point(cx, cy - 18 * scale),
        fitz.Point(cx - 36 * scale, cy + 70 * scale),
        fitz.Point(cx + 36 * scale, cy + 70 * scale),
    ]
    page.draw_polyline(cape, color=RED_DK, fill=CAPE, width=1.5, closePath=True)
    page.draw_circle(fitz.Point(cx, cy - 46 * scale), 20 * scale, color=RED_DK, fill=RED, width=1.2)


def _draw_basket(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    body = fitz.Rect(cx - 28 * scale, cy - 16 * scale, cx + 28 * scale, cy + 24 * scale)
    page.draw_rect(body, color=(0.35, 0.22, 0.12), fill=BASKET, width=1.2)
    page.draw_line(
        fitz.Point(cx - 20 * scale, cy - 16 * scale),
        fitz.Point(cx - 14 * scale, cy - 34 * scale),
        color=(0.35, 0.22, 0.12),
        width=3 * scale,
    )
    page.draw_line(
        fitz.Point(cx + 20 * scale, cy - 16 * scale),
        fitz.Point(cx + 14 * scale, cy - 34 * scale),
        color=(0.35, 0.22, 0.12),
        width=3 * scale,
    )


def _draw_wolf(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    body = fitz.Rect(cx - 52 * scale, cy - 20 * scale, cx + 52 * scale, cy + 36 * scale)
    page.draw_oval(body, color=WOLF_DK, fill=WOLF, width=1.5)
    head = fitz.Rect(cx + 30 * scale, cy - 42 * scale, cx + 78 * scale, cy + 8 * scale)
    page.draw_oval(head, color=WOLF_DK, fill=WOLF, width=1.5)
    page.draw_circle(fitz.Point(cx + 58 * scale, cy - 18 * scale), 5 * scale, color=None, fill=INK)
    page.draw_line(
        fitz.Point(cx + 72 * scale, cy - 8 * scale),
        fitz.Point(cx + 88 * scale, cy + 2 * scale),
        color=WOLF_DK,
        width=2,
    )


def _draw_cottage(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    base = fitz.Rect(cx - 70 * scale, cy, cx + 70 * scale, cy + 80 * scale)
    page.draw_rect(base, color=(0.35, 0.22, 0.12), fill=(0.78, 0.62, 0.42), width=1.5)
    roof = [
        fitz.Point(cx, cy - 50 * scale),
        fitz.Point(cx - 86 * scale, cy + 4 * scale),
        fitz.Point(cx + 86 * scale, cy + 4 * scale),
    ]
    page.draw_polyline(roof, color=(0.35, 0.18, 0.10), fill=(0.62, 0.20, 0.16), width=1.5, closePath=True)
    page.draw_rect(
        fitz.Rect(cx - 18 * scale, cy + 28 * scale, cx + 18 * scale, cy + 80 * scale),
        color=(0.25, 0.16, 0.10),
        fill=(0.35, 0.22, 0.12),
    )


def _draw_grandmother(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    page.draw_circle(
        fitz.Point(cx, cy - 30 * scale),
        18 * scale,
        color=(0.45, 0.30, 0.20),
        fill=SKIN,
        width=1.2,
    )
    shawl = [
        fitz.Point(cx, cy - 12 * scale),
        fitz.Point(cx - 34 * scale, cy + 64 * scale),
        fitz.Point(cx + 34 * scale, cy + 64 * scale),
    ]
    page.draw_polyline(shawl, color=(0.35, 0.22, 0.12), fill=WHITE, width=1.5, closePath=True)
    page.draw_circle(fitz.Point(cx, cy - 44 * scale), 20 * scale, color=(0.55, 0.50, 0.44), fill=(0.82, 0.78, 0.72))


def _cover_raster(width: int = 520, height: int = 300) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (214, 228, 206))
    d = ImageDraw.Draw(img)
    d.rectangle([0, int(height * 0.55), width, height], fill=(88, 120, 72))
    for x in (50, 140, 360, 450):
        d.polygon([(x, 60), (x - 30, 170), (x + 30, 170)], fill=(34, 84, 52))
        d.rectangle([x - 5, 170, x + 5, 188], fill=(90, 58, 30))
    hx, hy = int(width * 0.42), int(height * 0.58)
    d.ellipse([hx - 18, hy - 52, hx + 18, hy - 18], fill=(248, 210, 176))
    d.polygon([(hx, hy - 16), (hx - 34, hy + 58), (hx + 34, hy + 58)], fill=(198, 32, 42))
    d.ellipse([hx - 22, hy - 64, hx + 22, hy - 24], fill=(184, 28, 36))
    bx, by = int(width * 0.58), int(height * 0.72)
    d.rectangle([bx - 24, by - 12, bx + 24, by + 18], fill=(140, 96, 56))
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


def _paragraph(page: fitz.Page, text: str, y: float, height: float = 200.0, size: float = 15.0) -> None:
    page.insert_textbox(
        fitz.Rect(MARGIN, y, PAGE_W - MARGIN, y + height),
        text,
        fontname=BODY_FONT,
        fontsize=size,
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
            "Her grandmother had made her a little hood of red velvet, and because "
            "she wore it so often she was called Little Red Riding Hood. One day her "
            "mother said, \u201cTake this cake and bottle of wine to your grandmother; she "
            "is ill and weak, and they will do her good. Walk properly and do not "
            "leave the path, or you may fall and break the bottle.\u201d Little Red Riding "
            "Hood promised to obey, and set out through the wood with her basket."
        ),
        lambda page, panel: (
            _draw_trees(page, panel),
            _draw_red_riding_hood(page, panel.x0 + 180, panel.y0 + 180, scale=1.1),
            _draw_basket(page, panel.x0 + 250, panel.y0 + 220, scale=1.0),
        ),
    )

    _story_page(
        doc,
        "2. The wolf on the path",
        (
            "As she went, a Wolf met her. He wished to eat her, but dared not do so "
            "in the open wood where woodcutters were near. So he walked beside her "
            "and asked where she was going. \u201cTo my grandmother,\u201d she said, \u201cin the "
            "little house beyond the mill.\u201d The Wolf thought, \u201cThe old woman is a "
            "choice morsel; I must act craftily.\u201d He urged her to pick flowers for "
            "her grandmother, and while she wandered from the path he ran straight "
            "to the cottage and knocked at the door."
        ),
        lambda page, panel: (
            _draw_trees(page, panel),
            _draw_red_riding_hood(page, panel.x0 + 120, panel.y0 + 170, scale=1.0),
            _draw_wolf(page, panel.x1 - 170, panel.y0 + 190, scale=0.9),
        ),
    )

    _story_page(
        doc,
        "3. At grandmother's door",
        (
            "The Wolf slipped inside, devoured the poor grandmother, put on her "
            "nightcap, and lay down in her bed. When Little Red Riding Hood arrived "
            "she was surprised to find the door unlatched. She stepped in and went "
            "to the bed. \u201cOh, grandmother,\u201d she said, \u201cwhat big ears you have!\u201d "
            "\u201cThe better to hear you with.\u201d \u201cWhat big eyes you have!\u201d \u201cThe better "
            "to see you with.\u201d \u201cWhat big teeth you have!\u201d \u201cThe better to eat you "
            "with!\u201d And scarcely had the Wolf spoken when he sprang from the bed."
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 120, panel.y0 + 150, scale=1.0),
            _draw_grandmother(page, panel.x0 + 300, panel.y0 + 180, scale=0.9),
            _draw_wolf(page, panel.x1 - 150, panel.y0 + 200, scale=0.8),
        ),
    )

    _story_page(
        doc,
        "4. Saved from the wolf",
        (
            "A huntsman passing by heard the noise, rushed in, and with his axe "
            "freed Little Red Riding Hood and her grandmother from the Wolf. They "
            "filled the wolf's body with heavy stones; when he awoke and tried to "
            "flee, he sank and was seen no more. Little Red Riding Hood went home "
            "and from that time forward thought of her mother's words whenever she "
            "walked through the wood: stay on the path, and do not speak to strangers."
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 200, panel.y0 + 160, scale=1.1),
            _draw_red_riding_hood(page, panel.x0 + 120, panel.y0 + 200, scale=1.0),
            _draw_grandmother(page, panel.x1 - 150, panel.y0 + 190, scale=0.85),
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
    assert total_drawings >= 1, "expected at least one drawn vector illustration"
    assert total_images >= 1, "expected at least one embedded raster image"
    size_bytes = path.stat().st_size
    assert size_bytes < 5 * 1024 * 1024, f"PDF too large to commit: {size_bytes} bytes"

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
