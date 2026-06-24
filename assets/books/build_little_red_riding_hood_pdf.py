#!/usr/bin/env python3
"""Build Kinora's second bundled public-domain demo book as a multi-page PDF.

Story: **Little Red Riding Hood** — an abridged retelling of the Brothers Grimm
fairy tale (*Rotkäppchen*). Public-domain wording, small cast (Red, Grandmother,
Wolf), painterly woodland art direction.

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
CAPE = (0.82, 0.12, 0.16)
FOREST = (0.14, 0.36, 0.22)
FOREST_LT = (0.28, 0.52, 0.32)
SKY = (0.72, 0.86, 0.94)
WOLF = (0.42, 0.40, 0.38)
WOLF_DK = (0.28, 0.26, 0.24)
PARCHMENT = (0.97, 0.95, 0.89)
INK = (0.13, 0.12, 0.16)
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


def _draw_trees(page: fitz.Page, panel: fitz.Rect) -> None:
    _fill(page, SKY)
    page.draw_rect(
        fitz.Rect(panel.x0, panel.y0 + panel.height * 0.45, panel.x1, panel.y1),
        color=FOREST,
        fill=FOREST_LT,
    )
    for x in (panel.x0 + 40, panel.x0 + 120, panel.x1 - 100, panel.x1 - 30):
        page.draw_polyline(
            [fitz.Point(x, panel.y0 + 40), fitz.Point(x - 28, panel.y0 + 150), fitz.Point(x + 28, panel.y0 + 150)],
            color=FOREST,
            fill=FOREST,
            closePath=True,
        )
        page.draw_rect(
            fitz.Rect(x - 6, panel.y0 + 150, x + 6, panel.y0 + 190),
            color=(0.35, 0.22, 0.12),
            fill=(0.45, 0.28, 0.14),
        )


def _draw_red(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    page.draw_circle(fitz.Point(cx, cy - 34 * scale), 14 * scale, color=(0.5, 0.36, 0.26), fill=(0.97, 0.82, 0.66))
    cape = [
        fitz.Point(cx, cy - 18 * scale),
        fitz.Point(cx - 34 * scale, cy + 56 * scale),
        fitz.Point(cx + 34 * scale, cy + 56 * scale),
    ]
    page.draw_polyline(cape, color=RED_DK, fill=CAPE, closePath=True, width=1.5)
    page.draw_circle(fitz.Point(cx, cy - 48 * scale), 10 * scale, color=RED_DK, fill=RED)


def _draw_wolf(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    body = fitz.Rect(cx - 50 * scale, cy - 20 * scale, cx + 50 * scale, cy + 36 * scale)
    page.draw_oval(body, color=WOLF_DK, fill=WOLF, width=1.5)
    head = fitz.Rect(cx + 20 * scale, cy - 44 * scale, cx + 70 * scale, cy + 4 * scale)
    page.draw_oval(head, color=WOLF_DK, fill=WOLF, width=1.5)
    page.draw_circle(fitz.Point(cx + 52 * scale, cy - 22 * scale), 4 * scale, fill=INK)
    page.draw_circle(fitz.Point(cx + 62 * scale, cy - 22 * scale), 4 * scale, fill=INK)


def _draw_cottage(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    base = fitz.Rect(cx - 80 * scale, cy, cx + 80 * scale, cy + 70 * scale)
    page.draw_rect(base, color=(0.45, 0.28, 0.14), fill=(0.62, 0.40, 0.22))
    roof = [
        fitz.Point(cx - 95 * scale, cy),
        fitz.Point(cx, cy - 55 * scale),
        fitz.Point(cx + 95 * scale, cy),
    ]
    page.draw_polyline(roof, color=(0.35, 0.18, 0.10), fill=(0.55, 0.22, 0.16), closePath=True)
    page.draw_rect(
        fitz.Rect(cx - 18 * scale, cy + 24 * scale, cx + 18 * scale, cy + 70 * scale),
        color=(0.25, 0.14, 0.08),
        fill=(0.35, 0.20, 0.12),
    )


def _cover_raster() -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (520, 300), (231, 224, 196))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 520, 140], fill=(184, 218, 232))
    d.rectangle([0, 140, 520, 300], fill=(72, 132, 82))
    d.polygon([(260, 40), (200, 150), (320, 150)], fill=(178, 32, 52))
    d.ellipse([235, 95, 285, 145], fill=(248, 210, 188))
    d.rectangle([245, 145, 275, 210], fill=(198, 36, 56))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _title_page(doc: fitz.Document) -> None:
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    _fill(page, PARCHMENT)
    page.insert_textbox(
        fitz.Rect(MARGIN, 120, PAGE_W - MARGIN, 260),
        "Little Red Riding Hood",
        fontname=TITLE_FONT,
        fontsize=42,
        color=INK,
        align=fitz.TEXT_ALIGN_CENTER,
    )
    page.insert_textbox(
        fitz.Rect(MARGIN, 250, PAGE_W - MARGIN, 310),
        "An abridged Brothers Grimm fairy tale",
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
            "subject": "Public-domain demo book for Kinora",
            "keywords": "public-domain, Grimm, fairy tale, Kinora demo",
        }
    )
    _title_page(doc)
    _story_page(
        doc,
        "1. Through the woods",
        (
            "Once upon a time there was a sweet little girl whom everyone loved. "
            "Her grandmother gave her a little cap of red velvet, and because she "
            "would not wear anything else, she was called Little Red Riding Hood. "
            "One day her mother said, \"Take this cake and bottle of wine to your "
            "grandmother, who is ill. Walk properly and do not leave the path, or "
            "you may fall and break the bottle.\" Red Riding Hood promised to obey."
        ),
        lambda page, panel: (_draw_trees(page, panel), _draw_red(page, panel.x0 + 180, panel.y0 + 210)),
    )
    _story_page(
        doc,
        "2. The wolf on the path",
        (
            "When Red Riding Hood entered the wood, a wolf met her. He wished her "
            "good day, but thought to himself, \"What a tender young creature — "
            "what a nice plump mouthful!\" He asked where she was going. \"To my "
            "grandmother's,\" she said. The wolf thought, \"The old woman lives "
            "far from here; I can reach her first.\" So he ran ahead by the shorter "
            "way while the child went on, picking flowers."
        ),
        lambda page, panel: (
            _draw_trees(page, panel),
            _draw_red(page, panel.x0 + 110, panel.y0 + 220, 0.9),
            _draw_wolf(page, panel.x1 - 120, panel.y0 + 210, 1.0),
        ),
    )
    _story_page(
        doc,
        "3. At grandmother's door",
        (
            "The wolf knocked at the cottage door. \"Who is there?\" called the "
            "grandmother. \"Little Red Riding Hood,\" said the wolf, disguising his "
            "voice, \"with cake and wine.\" The door was unlatched, and the wolf "
            "sprang upon the bed. When Red Riding Hood arrived, she thought her "
            "grandmother looked very strange. \"Grandmother, what big eyes you have!\" "
            "\"The better to see you with, my child.\" \"What big teeth you have!\" "
            "\"The better to eat you with!\""
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 200, panel.y0 + 250, 1.1),
            _draw_wolf(page, panel.x0 + 200, panel.y0 + 180, 0.8),
        ),
    )
    _story_page(
        doc,
        "4. The huntsman",
        (
            "The wolf had scarcely swallowed Red Riding Hood when a huntsman passed "
            "by the cottage. He thought the old woman snored strangely and looked "
            "inside. He cut open the wolf's belly with his scissors, and Red Riding "
            "Hood sprang out, frightened but unhurt. They filled the wolf's stomach "
            "with heavy stones; when he woke and tried to flee, he sank and was never "
            "seen again. Red Riding Hood went home, and never again left the path."
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 200, panel.y0 + 260, 1.0),
            _draw_red(page, panel.x0 + 140, panel.y0 + 220, 0.85),
            page.draw_rect(
                fitz.Rect(panel.x1 - 90, panel.y0 + 150, panel.x1 - 30, panel.y0 + 280),
                color=(0.25, 0.20, 0.18),
                fill=(0.38, 0.32, 0.28),
            ),
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
    print("Built + verified Little Red Riding Hood:")
    for key, value in info.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
