#!/usr/bin/env python3
"""Build Kinora's second bundled public-domain demo book as a real PDF.

Story: **"Little Red Riding Hood"** — an abridged retelling of the Brothers Grimm
fairy tale (*Rotkäppchen*, Kinder- und Hausmärchen, 1812). The tale and its
19th-century English translations are firmly in the **public domain**.

Like ``build_demo_pdf.py``, this produces a small, demo-friendly book:

* a title page + four story pages (five total),
* three characters the ingest canon will lock — **Little Red Riding Hood**,
  the **Wolf**, and **Grandmother**,
* a selectable text layer on every page, and
* simple vector illustrations plus one embedded raster cover scene.

Run::

    backend/.venv/bin/python assets/books/build_little_red_riding_hood.py
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
WOLF = (0.42, 0.40, 0.38)
WOLF_DK = (0.24, 0.22, 0.20)
FOREST = (0.18, 0.40, 0.28)
FOREST_DK = (0.10, 0.26, 0.16)
COTTAGE = (0.78, 0.62, 0.38)
COTTAGE_DK = (0.52, 0.38, 0.22)
INK = (0.13, 0.12, 0.16)
PARCHMENT = (0.97, 0.95, 0.89)
SKIN = (0.97, 0.82, 0.66)
HAIR = (0.92, 0.78, 0.42)

TITLE_FONT = "times-bold"
BODY_FONT = "times-roman"
ITALIC_FONT = "times-italic"


def _fill_background(page: fitz.Page, color: tuple[float, float, float]) -> None:
    page.draw_rect(page.rect, color=color, fill=color)


def _draw_trees(page: fitz.Page, panel: fitz.Rect) -> None:
    for x in (panel.x0 + 40, panel.x0 + 120, panel.x1 - 100, panel.x1 - 30):
        page.draw_polyline(
            [fitz.Point(x, panel.y0 + 30), fitz.Point(x - 28, panel.y0 + 120), fitz.Point(x + 28, panel.y0 + 120)],
            color=FOREST_DK,
            fill=FOREST,
            width=1.2,
            closePath=True,
        )
        page.draw_rect(fitz.Rect(x - 5, panel.y0 + 118, x + 5, panel.y0 + 140), color=COTTAGE_DK, fill=COTTAGE_DK)


def _draw_red_riding_hood(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    gown = [
        fitz.Point(cx, cy - 18 * scale),
        fitz.Point(cx - 36 * scale, cy + 64 * scale),
        fitz.Point(cx + 36 * scale, cy + 64 * scale),
    ]
    page.draw_polyline(gown, color=RED_DK, fill=RED, width=1.5, closePath=True)
    page.draw_circle(fitz.Point(cx, cy - 32 * scale), 14 * scale, color=(0.5, 0.36, 0.26), fill=SKIN, width=1.2)
    hood = [
        fitz.Point(cx, cy - 48 * scale),
        fitz.Point(cx - 22 * scale, cy - 18 * scale),
        fitz.Point(cx + 22 * scale, cy - 18 * scale),
    ]
    page.draw_polyline(hood, color=RED_DK, fill=RED, width=1.2, closePath=True)
    page.draw_circle(fitz.Point(cx, cy - 34 * scale), 5 * scale, color=None, fill=HAIR)


def _draw_basket(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    body = fitz.Rect(cx - 22 * scale, cy - 10 * scale, cx + 22 * scale, cy + 24 * scale)
    page.draw_rect(body, color=COTTAGE_DK, fill=COTTAGE, width=1.2)
    page.draw_bezier(
        fitz.Point(cx - 22 * scale, cy - 10 * scale),
        fitz.Point(cx - 22 * scale, cy - 28 * scale),
        fitz.Point(cx + 22 * scale, cy - 28 * scale),
        fitz.Point(cx + 22 * scale, cy - 10 * scale),
        color=COTTAGE_DK,
        width=2,
    )


def _draw_wolf(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    body = fitz.Rect(cx - 52 * scale, cy - 20 * scale, cx + 52 * scale, cy + 36 * scale)
    page.draw_oval(body, color=WOLF_DK, fill=WOLF, width=1.5)
    head = fitz.Rect(cx + 34 * scale, cy - 34 * scale, cx + 86 * scale, cy + 18 * scale)
    page.draw_oval(head, color=WOLF_DK, fill=WOLF, width=1.5)
    page.draw_circle(fitz.Point(cx + 62 * scale, cy - 10 * scale), 4 * scale, color=None, fill=INK)
    page.draw_polyline(
        [
            fitz.Point(cx + 86 * scale, cy - 4 * scale),
            fitz.Point(cx + 104 * scale, cy + 8 * scale),
            fitz.Point(cx + 82 * scale, cy + 10 * scale),
        ],
        color=WOLF_DK,
        fill=WOLF,
        closePath=True,
    )
    for dx in (-36 * scale, 12 * scale):
        page.draw_line(
            fitz.Point(cx + dx, cy + 30 * scale),
            fitz.Point(cx + dx * 1.1, cy + 54 * scale),
            color=WOLF_DK,
            width=4 * scale,
        )


def _draw_cottage(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    w = 120 * scale
    h = 80 * scale
    body = fitz.Rect(cx - w / 2, cy, cx + w / 2, cy + h)
    page.draw_rect(body, color=COTTAGE_DK, fill=COTTAGE, width=1.5)
    roof = [
        fitz.Point(cx - w / 2 - 10 * scale, cy),
        fitz.Point(cx, cy - 50 * scale),
        fitz.Point(cx + w / 2 + 10 * scale, cy),
    ]
    page.draw_polyline(roof, color=RED_DK, fill=RED, width=1.5, closePath=True)
    door = fitz.Rect(cx - 16 * scale, cy + 28 * scale, cx + 16 * scale, cy + h)
    page.draw_rect(door, color=COTTAGE_DK, fill=(0.35, 0.22, 0.14))
    page.draw_rect(fitz.Rect(cx + 24 * scale, cy + 24 * scale, cx + 44 * scale, cy + 44 * scale), color=INK, fill=(0.72, 0.86, 0.96))


def _draw_grandmother(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    page.draw_circle(fitz.Point(cx, cy - 24 * scale), 16 * scale, color=(0.5, 0.36, 0.26), fill=SKIN, width=1.2)
    shawl = [
        fitz.Point(cx, cy - 8 * scale),
        fitz.Point(cx - 42 * scale, cy + 58 * scale),
        fitz.Point(cx + 42 * scale, cy + 58 * scale),
    ]
    page.draw_polyline(shawl, color=RED_DK, fill=(0.82, 0.22, 0.28), width=1.5, closePath=True)
    for dx in (-10 * scale, 10 * scale):
        page.draw_line(
            fitz.Point(cx + dx, cy - 30 * scale),
            fitz.Point(cx + dx * 2.2, cy - 46 * scale),
            color=INK,
            width=2,
        )


def _cover_raster(width: int = 520, height: int = 300) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (231, 224, 196))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, int(height * 0.62)], fill=(206, 224, 213))
    d.rectangle([0, int(height * 0.62), width, height], fill=(120, 150, 96))
    for x in (50, 140, width - 120, width - 40):
        d.polygon([(x, 70), (x - 34, 190), (x + 34, 190)], fill=(40, 92, 60))
        d.rectangle([x - 6, 190, x + 6, 210], fill=(80, 55, 30))
    # Cottage.
    cx, cy = int(width * 0.68), int(height * 0.62)
    d.rectangle([cx - 55, cy, cx + 55, cy + 70], fill=(198, 158, 96), outline=(120, 86, 46), width=3)
    d.polygon([(cx - 65, cy), (cx, cy - 45), (cx + 65, cy)], fill=(184, 36, 46))
    # Red riding hood figure.
    rx, ry = int(width * 0.28), int(height * 0.58)
    d.polygon([(rx, ry - 20), (rx - 28, ry + 50), (rx + 28, ry + 50)], fill=(184, 36, 46))
    d.ellipse([rx - 14, ry - 42, rx + 14, ry - 16], fill=(247, 209, 168))
    d.polygon([(rx, ry - 58), (rx - 18, ry - 22), (rx + 18, ry - 22)], fill=(184, 36, 46))
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
            "Her grandmother had made her a little red riding hood, and because she "
            "looked so charming in it, she was called Little Red Riding Hood. One day "
            "her mother said, \"Take this cake and bottle of wine to your grandmother; "
            "she is ill and weak, and they will do her good. Walk properly and do not "
            "leave the path, or you may fall and break the bottle.\" Little Red Riding "
            "Hood promised to obey, and set out through the wood with her basket."
        ),
        lambda page, panel: (
            _draw_trees(page, panel),
            _draw_red_riding_hood(page, panel.x0 + 150, panel.y0 + 210, scale=1.1),
            _draw_basket(page, panel.x0 + 110, panel.y0 + 250, scale=0.9),
        ),
    )

    _story_page(
        doc,
        "2. The wolf on the path",
        (
            "Little Red Riding Hood had not gone far when a wolf met her. He wished her "
            "good day, and asked where she was going. \"To my grandmother's,\" she said, "
            "\"who lives in the little house beyond the mill.\" The wolf thought to himself, "
            "\"What a tender young creature — she will be a delicate morsel.\" But he dared "
            "not eat her, for there were woodcutters nearby. So he walked beside her and "
            "said, \"See what pretty flowers grow here! Why do you not look around?\" "
            "Little Red Riding Hood raised her eyes and, seeing the sunbeams dancing "
            "through the trees, ran from the path to pick a nosegay for her grandmother."
        ),
        lambda page, panel: (
            _draw_trees(page, panel),
            _draw_red_riding_hood(page, panel.x0 + 110, panel.y0 + 220, scale=1.0),
            _draw_wolf(page, panel.x1 - 180, panel.y0 + 230, scale=0.9),
        ),
    )

    _story_page(
        doc,
        "3. At grandmother's door",
        (
            "Meanwhile the wolf ran straight to the grandmother's house and knocked. "
            "\"Who is there?\" \"Little Red Riding Hood,\" answered the wolf, disguising "
            "his voice. \"Lift the latch and come in,\" said the old woman. The wolf "
            "sprang in, swallowed the grandmother at one gulp, put on her cap, drew the "
            "curtains, and lay down in her bed. When Little Red Riding Hood arrived, "
            "she was surprised to find the cottage door standing open. She went to the "
            "bed and drew back the curtains. There lay her grandmother, but she looked "
            "very strange."
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 220, panel.y0 + 250, scale=1.2),
            _draw_wolf(page, panel.x0 + 360, panel.y0 + 300, scale=0.55),
        ),
    )

    _story_page(
        doc,
        "4. The woodcutter's rescue",
        (
            "\"Oh, grandmother,\" she said, \"what big ears you have!\" \"The better to "
            "hear you with.\" \"What big eyes you have!\" \"The better to see you with.\" "
            "\"What big hands you have!\" \"The better to seize you with!\" \"What a "
            "dreadful big mouth you have!\" \"The better to eat you with!\" And scarcely "
            "had the wolf said this than he sprang out of bed and swallowed Little Red "
            "Riding Hood. A huntsman passing by heard the snoring, looked in, and cut "
            "open the wolf with his knife. Little Red Riding Hood and her grandmother "
            "sprang out, unharmed, and there was great joy in the cottage."
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 200, panel.y0 + 260, scale=1.0),
            _draw_grandmother(page, panel.x0 + 130, panel.y0 + 250, scale=0.9),
            _draw_red_riding_hood(page, panel.x0 + 300, panel.y0 + 260, scale=0.85),
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
