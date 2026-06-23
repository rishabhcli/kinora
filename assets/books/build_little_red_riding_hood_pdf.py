#!/usr/bin/env python3
"""Build Kinora's second bundled public-domain demo book as a multi-page PDF.

Story: **"Little Red Riding Hood"** — an abridged retelling of the Brothers
Grimm tale (*Rotkäppchen*, 1812). Public-domain wording, three principal
characters (**Little Red**, the **Wolf**, and **Grandmother**), selectable
text on every page, and simple vector illustrations.

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
CAPE = (0.82, 0.12, 0.16)
WOLF = (0.42, 0.40, 0.38)
WOLF_DK = (0.28, 0.26, 0.24)
FOREST = (0.18, 0.42, 0.28)
FOREST_DK = (0.10, 0.28, 0.18)
COTTAGE = (0.78, 0.62, 0.40)
COTTAGE_DK = (0.52, 0.38, 0.22)
INK = (0.13, 0.12, 0.16)
PARCHMENT = (0.97, 0.95, 0.89)
SKIN = (0.97, 0.82, 0.66)
HAIR = (0.92, 0.78, 0.48)

TITLE_FONT = "times-bold"
BODY_FONT = "times-roman"
ITALIC_FONT = "times-italic"


def _fill_background(page: fitz.Page, color: tuple[float, float, float]) -> None:
    page.draw_rect(page.rect, color=color, fill=color)


def _heading(page: fitz.Page, text: str, y: float) -> None:
    page.insert_textbox(
        fitz.Rect(MARGIN, y, PAGE_W - MARGIN, y + 44),
        text,
        fontname=TITLE_FONT,
        fontsize=24,
        color=INK,
    )


def _paragraph(page: fitz.Page, text: str, y: float, height: float = 200.0) -> None:
    page.insert_textbox(
        fitz.Rect(MARGIN, y, PAGE_W - MARGIN, y + height),
        text,
        fontname=BODY_FONT,
        fontsize=15,
        color=INK,
        lineheight=1.5,
    )


def _draw_tree(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    page.draw_rect(
        fitz.Rect(cx - 5 * scale, cy, cx + 5 * scale, cy + 50 * scale),
        color=FOREST_DK,
        fill=FOREST_DK,
    )
    page.draw_circle(fitz.Point(cx, cy - 10 * scale), 28 * scale, color=FOREST_DK, fill=FOREST)


def _draw_little_red(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    page.draw_circle(
        fitz.Point(cx, cy - 34 * scale),
        16 * scale,
        color=(0.5, 0.36, 0.26),
        fill=SKIN,
    )
    cape = [
        fitz.Point(cx, cy - 18 * scale),
        fitz.Point(cx - 36 * scale, cy + 70 * scale),
        fitz.Point(cx + 36 * scale, cy + 70 * scale),
    ]
    page.draw_polyline(cape, color=RED_DK, fill=CAPE, closePath=True)
    hood = fitz.Rect(cx - 22 * scale, cy - 52 * scale, cx + 22 * scale, cy - 20 * scale)
    page.draw_oval(hood, color=RED_DK, fill=RED)


def _draw_wolf(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    body = fitz.Rect(cx - 50 * scale, cy - 20 * scale, cx + 50 * scale, cy + 40 * scale)
    page.draw_oval(body, color=WOLF_DK, fill=WOLF)
    page.draw_circle(fitz.Point(cx + 38 * scale, cy - 10 * scale), 22 * scale, color=WOLF_DK, fill=WOLF)
    for ex in (cx + 28 * scale, cx + 46 * scale):
        page.draw_circle(fitz.Point(ex, cy - 18 * scale), 4 * scale, fill=INK)
    page.draw_circle(fitz.Point(cx + 52 * scale, cy + 2 * scale), 5 * scale, fill=INK)


def _draw_grandmother(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    page.draw_circle(fitz.Point(cx, cy - 30 * scale), 18 * scale, fill=SKIN, color=(0.5, 0.36, 0.26))
    hair = fitz.Rect(cx - 24 * scale, cy - 54 * scale, cx + 24 * scale, cy - 24 * scale)
    page.draw_oval(hair, color=(0.5, 0.36, 0.26), fill=HAIR)
    gown = [
        fitz.Point(cx, cy - 12 * scale),
        fitz.Point(cx - 42 * scale, cy + 72 * scale),
        fitz.Point(cx + 42 * scale, cy + 72 * scale),
    ]
    page.draw_polyline(gown, color=(0.35, 0.35, 0.42), fill=(0.62, 0.64, 0.72), closePath=True)


def _draw_cottage(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    body = fitz.Rect(cx - 80 * scale, cy - 20 * scale, cx + 80 * scale, cy + 80 * scale)
    page.draw_rect(body, color=COTTAGE_DK, fill=COTTAGE)
    roof = [
        fitz.Point(cx - 95 * scale, cy - 20 * scale),
        fitz.Point(cx, cy - 90 * scale),
        fitz.Point(cx + 95 * scale, cy - 20 * scale),
    ]
    page.draw_polyline(roof, color=(0.35, 0.18, 0.12), fill=(0.55, 0.22, 0.16), closePath=True)
    page.draw_rect(
        fitz.Rect(cx - 18 * scale, cy + 20 * scale, cx + 18 * scale, cy + 80 * scale),
        color=(0.25, 0.16, 0.10),
        fill=(0.38, 0.24, 0.14),
    )


def _cover_raster(width: int = 520, height: int = 300) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (206, 224, 213))
    d = ImageDraw.Draw(img)
    d.rectangle([0, int(height * 0.45), width, height], fill=(120, 150, 96))
    for x in (70, 180, 360, 450):
        d.rectangle([x - 5, int(height * 0.55), x + 5, int(height * 0.72)], fill=(80, 55, 30))
        d.ellipse([x - 28, int(height * 0.32), x + 28, int(height * 0.58)], fill=(40, 92, 60))
    d.polygon([(int(width * 0.72), 90), (int(width * 0.55), 170), (int(width * 0.89), 170)], fill=(140, 100, 60))
    d.rectangle([int(width * 0.58), 170, int(width * 0.86), 230], fill=(198, 158, 102))
    bx, by = int(width * 0.22), int(height * 0.58)
    d.ellipse([bx - 14, by - 40, bx + 14, by - 12], fill=(247, 209, 168))
    d.polygon([(bx, by - 8), (bx - 30, by + 50), (bx + 30, by + 50)], fill=(210, 30, 45))
    d.ellipse([bx - 24, by - 52, bx + 24, by - 22], fill=(210, 30, 45))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


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
        fitz.Rect(MARGIN, 120, PAGE_W - MARGIN, 240),
        "Little Red\nRiding Hood",
        fontname=TITLE_FONT,
        fontsize=42,
        color=INK,
        align=fitz.TEXT_ALIGN_CENTER,
    )
    title.insert_textbox(
        fitz.Rect(MARGIN, 250, PAGE_W - MARGIN, 310),
        "An abridged retelling of the Brothers Grimm fairy tale",
        fontname=ITALIC_FONT,
        fontsize=17,
        color=(0.40, 0.36, 0.31),
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
        color=(0.40, 0.36, 0.31),
        align=fitz.TEXT_ALIGN_CENTER,
    )

    _story_page(
        doc,
        "1. Into the woods",
        (
            "Once upon a time there was a sweet little girl whom everyone loved. Her "
            "grandmother gave her a little cap of red velvet, and because it suited her "
            "so well she was always called Little Red Riding Hood. One day her mother "
            "said, \"Take this cake and bottle of wine to your grandmother, who is ill "
            "and weak. Go straight there and do not leave the path, or you may fall and "
            "break the bottle.\" Little Red Riding Hood promised to obey, and set out "
            "through the wood with her basket."
        ),
        lambda page, panel: (
            _draw_tree(page, panel.x0 + 80, panel.y0 + 200),
            _draw_tree(page, panel.x1 - 90, panel.y0 + 180, 1.1),
            _draw_little_red(page, panel.x0 + 250, panel.y0 + 170, 1.1),
        ),
    )

    _story_page(
        doc,
        "2. The wolf on the path",
        (
            "The wolf met her on the way. He wished to eat her, but dared not because "
            "woodcutters were near. So he asked politely where she was going. \"To my "
            "grandmother's,\" she said, \"beyond the mill, under three great oak-trees.\" "
            "The wolf thought, \"The tender young creature will make a dainty morsel.\" "
            "He walked beside her a while, then said, \"See what pretty flowers grow "
            "here! Why do you not look about?\" Little Red Riding Hood left the path "
            "to pick blossoms, and the wolf ran straight to the grandmother's house."
        ),
        lambda page, panel: (
            _draw_tree(page, panel.x0 + 60, panel.y0 + 190),
            _draw_little_red(page, panel.x0 + 180, panel.y0 + 180),
            _draw_wolf(page, panel.x1 - 150, panel.y0 + 190, 1.0),
        ),
    )

    _story_page(
        doc,
        "3. At grandmother's door",
        (
            "The wolf knocked at the door. \"Who is there?\" called the grandmother. "
            "\"Little Red Riding Hood,\" said the wolf, disguising his voice. \"Pull the "
            "bobbin and the latch will open.\" He sprang upon the bed and swallowed the "
            "grandmother whole. Then he put on her cap, drew the curtains, and waited. "
            "Soon Little Red Riding Hood arrived with her basket. She was surprised to "
            "find the door standing open, and everything in the cottage seemed strange."
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 220, panel.y0 + 210, 1.0),
            _draw_wolf(page, panel.x0 + 220, panel.y0 + 150, 0.75),
        ),
    )

    _story_page(
        doc,
        "4. All is well again",
        (
            "\"Grandmother, what big eyes you have!\" \"The better to see you with, my "
            "child.\" \"Grandmother, what big teeth you have!\" \"The better to eat you "
            "with!\" cried the wolf, and leaped from the bed. At that moment a huntsman "
            "passed by, heard the noise, and rushed in. He cut open the sleeping wolf, "
            "and out stepped Little Red Riding Hood and her grandmother, frightened but "
            "unharmed. They filled the wolf's belly with stones; when he woke he could "
            "not run away, and everyone was safe."
        ),
        lambda page, panel: (
            _draw_grandmother(page, panel.x0 + 120, panel.y0 + 170, 1.0),
            _draw_little_red(page, panel.x0 + 250, panel.y0 + 180, 1.0),
            _draw_wolf(page, panel.x1 - 140, panel.y0 + 220, 0.9),
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

    assert num_pages >= 4
    assert total_words >= 200
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
