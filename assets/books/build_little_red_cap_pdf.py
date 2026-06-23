#!/usr/bin/env python3
"""Build Kinora's second bundled public-domain demo book as a multi-page PDF.

Story: **"Little Red Riding Hood"** — an abridged retelling of the Brothers
Grimm fairy tale (*Rotkäppchen*, Kinder- und Hausmärchen, 1812). Public domain.

Deliberately small and demo-friendly: title page + four story pages, three
characters for the ingest canon (Little Red, the Wolf, Grandmother), selectable
text on every page, and simple vector illustrations.

Run::

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

RED = (0.72, 0.14, 0.18)
RED_DK = (0.48, 0.08, 0.12)
CAPE = (0.82, 0.12, 0.16)
WOLF = (0.42, 0.40, 0.38)
WOLF_DK = (0.28, 0.26, 0.24)
FOREST = (0.18, 0.38, 0.24)
FOREST_DK = (0.10, 0.24, 0.14)
PARCHMENT = (0.97, 0.95, 0.89)
INK = (0.13, 0.12, 0.16)
STONE = (0.55, 0.50, 0.44)
GOLD = (0.86, 0.68, 0.18)

TITLE_FONT = "times-bold"
BODY_FONT = "times-roman"
ITALIC_FONT = "times-italic"


def _fill(page: fitz.Page, color: tuple[float, float, float]) -> None:
    page.draw_rect(page.rect, color=color, fill=color)


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


def _draw_red(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    """Little Red: hooded figure in a red cape."""
    hood = fitz.Rect(cx - 28 * scale, cy - 50 * scale, cx + 28 * scale, cy - 8 * scale)
    page.draw_oval(hood, color=RED_DK, fill=CAPE, width=1.5)
    page.draw_circle(
        fitz.Point(cx, cy - 20 * scale),
        14 * scale,
        color=(0.5, 0.36, 0.26),
        fill=(0.97, 0.82, 0.66),
        width=1.2,
    )
    body = fitz.Rect(cx - 22 * scale, cy - 6 * scale, cx + 22 * scale, cy + 60 * scale)
    page.draw_rect(body, color=RED_DK, fill=RED, width=1.2)


def _draw_wolf(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    """A grey wolf: snout, ears, tail."""
    body = fitz.Rect(cx - 50 * scale, cy - 20 * scale, cx + 50 * scale, cy + 40 * scale)
    page.draw_oval(body, color=WOLF_DK, fill=WOLF, width=1.5)
    head = fitz.Rect(cx + 30 * scale, cy - 36 * scale, cx + 80 * scale, cy + 10 * scale)
    page.draw_oval(head, color=WOLF_DK, fill=WOLF, width=1.5)
    for dx in (38 * scale, 62 * scale):
        ear = fitz.Rect(cx + dx - 8 * scale, cy - 52 * scale, cx + dx + 8 * scale, cy - 28 * scale)
        page.draw_oval(ear, color=WOLF_DK, fill=WOLF, width=1.0)
    page.draw_circle(fitz.Point(cx + 68 * scale, cy - 10 * scale), 4 * scale, fill=INK)
    page.draw_line(
        fitz.Point(cx - 48 * scale, cy + 10 * scale),
        fitz.Point(cx - 80 * scale, cy + 30 * scale),
        color=WOLF_DK,
        width=5 * scale,
    )


def _draw_cottage(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    """Grandmother's cottage with a chimney."""
    base = fitz.Rect(cx - 80 * scale, cy, cx + 80 * scale, cy + 70 * scale)
    page.draw_rect(base, color=STONE, fill=(0.88, 0.82, 0.72), width=1.5)
    roof = [
        fitz.Point(cx - 95 * scale, cy),
        fitz.Point(cx, cy - 55 * scale),
        fitz.Point(cx + 95 * scale, cy),
    ]
    page.draw_polyline(roof, color=RED_DK, fill=RED, width=1.5, closePath=True)
    page.draw_rect(
        fitz.Rect(cx + 40 * scale, cy - 80 * scale, cx + 58 * scale, cy - 20 * scale),
        color=STONE,
        fill=STONE,
        width=1.0,
    )
    page.draw_rect(
        fitz.Rect(cx - 20 * scale, cy + 20 * scale, cx + 20 * scale, cy + 70 * scale),
        color=(0.35, 0.24, 0.14),
        fill=(0.45, 0.30, 0.18),
        width=1.0,
    )


def _draw_trees(page: fitz.Page, panel: fitz.Rect) -> None:
    for x in (panel.x0 + 60, panel.x0 + 140, panel.x1 - 100):
        page.draw_polyline(
            [
                fitz.Point(x, panel.y0 + 30),
                fitz.Point(x - 30, panel.y0 + 120),
                fitz.Point(x + 30, panel.y0 + 120),
            ],
            color=FOREST_DK,
            fill=FOREST,
            width=1.2,
            closePath=True,
        )
        page.draw_rect(
            fitz.Rect(x - 6, panel.y0 + 120, x + 6, panel.y0 + 150),
            color=(0.35, 0.22, 0.12),
            fill=(0.55, 0.36, 0.20),
        )


def _cover_raster() -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (520, 300), (231, 224, 196))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 520, 180], fill=(120, 150, 96))
    for x in (80, 200, 380):
        d.polygon([(x, 40), (x - 34, 130), (x + 34, 130)], fill=(40, 92, 60))
        d.rectangle([x - 6, 130, x + 6, 150], fill=(80, 55, 30))
    # Red figure
    d.ellipse([180, 90, 240, 200], fill=(210, 30, 46))
    d.ellipse([195, 70, 225, 110], fill=(248, 210, 170))
    # Wolf peeking
    d.ellipse([320, 120, 420, 180], fill=(108, 102, 98))
    d.ellipse([390, 110, 450, 150], fill=(108, 102, 98))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _title_page(doc: fitz.Document) -> None:
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    _fill(page, PARCHMENT)
    page.insert_textbox(
        fitz.Rect(MARGIN, 120, PAGE_W - MARGIN, 240),
        "Little Red Riding Hood",
        fontname=TITLE_FONT,
        fontsize=40,
        color=INK,
        align=fitz.TEXT_ALIGN_CENTER,
    )
    page.insert_textbox(
        fitz.Rect(MARGIN, 240, PAGE_W - MARGIN, 300),
        "An abridged retelling of the Brothers Grimm fairy tale",
        fontname=ITALIC_FONT,
        fontsize=17,
        color=STONE,
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
        color=STONE,
        align=fitz.TEXT_ALIGN_CENTER,
    )


def _story_page(doc: fitz.Document, heading: str, body: str, illustrate) -> None:
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    _fill(page, PARCHMENT)
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
            "keywords": "public-domain, Grimm, fairy tale, Kinora demo",
        }
    )

    _title_page(doc)

    _story_page(
        doc,
        "1. Into the forest",
        (
            "Once upon a time there was a sweet little girl whom everyone loved. "
            "Her grandmother had made her a little red velvet cap, and she wore it "
            "so often that she was called Little Red Riding Hood. One day her mother "
            "said, \"Take this cake and bottle of wine to your grandmother; she is "
            "ill and weak, and they will do her good. Walk properly and do not leave "
            "the path, or you may fall and break the bottle.\" Little Red Riding Hood "
            "promised to obey, and set out through the wood."
        ),
        lambda page, panel: (
            _draw_trees(page, panel),
            _draw_red(page, panel.x0 + 200, panel.y0 + 180, scale=1.1),
        ),
    )

    _story_page(
        doc,
        "2. The wolf on the path",
        (
            "She had not gone far when the Wolf met her. He wished her good day, "
            "but he thought, \"What a tender young creature! She will be a nice morsel.\" "
            "Aloud he asked where she was going. \"To my grandmother, who lives "
            "under the three great oak-trees.\" The Wolf thought of a plan. He ran "
            "straight to the grandmother's house and knocked. When the door opened, "
            "he sprang in and swallowed the poor old woman at one gulp."
        ),
        lambda page, panel: (
            _draw_trees(page, panel),
            _draw_red(page, panel.x0 + 120, panel.y0 + 200, scale=1.0),
            _draw_wolf(page, panel.x1 - 160, panel.y0 + 190, scale=0.9),
        ),
    )

    _story_page(
        doc,
        "3. At grandmother's door",
        (
            "Little Red Riding Hood gathered flowers along the way and arrived late. "
            "She knocked, and a rough voice answered, \"Who is there?\" \"Little Red "
            "Riding Hood, with cake and wine. Open the door.\" \"Lift the latch and "
            "come in,\" said the Wolf, disguising his voice. She entered, and found "
            "the curtains drawn. \"Grandmother, what big ears you have!\" \"The better "
            "to hear you with.\" \"Grandmother, what big eyes you have!\" \"The better "
            "to see you with.\" \"Grandmother, what big teeth you have!\" \"The better "
            "to eat you with!\" And the Wolf leaped from the bed."
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 200, panel.y0 + 180, scale=1.0),
            _draw_red(page, panel.x0 + 120, panel.y0 + 260, scale=0.85),
        ),
    )

    _story_page(
        doc,
        "4. The rescue",
        (
            "A huntsman passing by heard the noise and stepped inside. He saw the Wolf "
            "and knew the old woman was in danger. He took his knife, cut open the "
            "sleeping Wolf, and out stepped Little Red Riding Hood and her grandmother, "
            "both unharmed. They filled the Wolf's body with heavy stones; when he woke "
            "and tried to run away, the stones dragged him down. Little Red Riding Hood "
            "thanked the huntsman, and she never again left the path to pick flowers "
            "while on an errand."
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 220, panel.y0 + 170, scale=0.9),
            _draw_red(page, panel.x0 + 140, panel.y0 + 250, scale=0.9),
            page.draw_circle(fitz.Point(panel.x1 - 120, panel.y0 + 120), 18, color=GOLD, fill=GOLD),
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
