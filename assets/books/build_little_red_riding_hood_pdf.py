#!/usr/bin/env python3
"""Build Kinora's second bundled public-domain demo book as a real PDF.

Story: **"Little Red Riding Hood"** — an abridged retelling of the Brothers
Grimm fairy tale (*Rotkäppchen*, Kinder- und Hausmärchen, 1812). The tale and
its 19th-century English translations are firmly in the **public domain**.

Like ``build_demo_pdf.py``, this book is deliberately small and demo-friendly:

* a title page + four story pages (five total),
* three characters the ingest canon will lock — **Little Red Riding Hood**,
  the **Wolf**, and **Grandmother**,
* a selectable text layer on every page, and
* simple vector illustrations drawn with PyMuPDF.

Run::

    backend/.venv/bin/python assets/books/build_little_red_riding_hood_pdf.py
"""

from __future__ import annotations

import io
from pathlib import Path

import fitz  # PyMuPDF

HERE = Path(__file__).resolve().parent
OUT_PATH = HERE / "little_red_riding_hood.pdf"

PAGE_W, PAGE_H = 720.0, 960.0
MARGIN = 64.0

INK = (0.13, 0.12, 0.16)
PARCHMENT = (0.97, 0.95, 0.89)
FOREST = (0.20, 0.42, 0.30)
FOREST_DK = (0.12, 0.28, 0.20)
CAPE = (0.72, 0.12, 0.16)
CAPE_HI = (0.92, 0.28, 0.32)
CAPE_DK = (0.52, 0.08, 0.12)
WOLF = (0.42, 0.40, 0.44)
WOLF_DK = (0.28, 0.26, 0.30)
COTTAGE = (0.86, 0.72, 0.48)
COTTAGE_DK = (0.62, 0.48, 0.30)
SKIN = (0.96, 0.84, 0.74)
HAIR = (0.92, 0.78, 0.42)

TITLE_FONT = "times-bold"
BODY_FONT = "times-roman"
ITALIC_FONT = "times-italic"


def _fill_background(page: fitz.Page, color: tuple[float, float, float]) -> None:
    page.draw_rect(page.rect, color=color, fill=color)


def _draw_forest(page: fitz.Page, panel: fitz.Rect) -> None:
    page.draw_rect(panel, color=FOREST_DK, fill=FOREST, width=1.0)
    for x in (panel.x0 + 50, panel.x0 + 150, panel.x1 - 120, panel.x1 - 40):
        page.draw_line(fitz.Point(x, panel.y0 + 30), fitz.Point(x, panel.y1 - 20), color=FOREST_DK, width=6)
        page.draw_circle(fitz.Point(x, panel.y0 + 20), 28, color=FOREST_DK, fill=(0.28, 0.58, 0.38))


def _draw_red_riding_hood(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    s = scale
    page.draw_circle(fitz.Point(cx, cy - 42 * s), 18 * s, color=INK, fill=SKIN)
    page.draw_circle(fitz.Point(cx, cy - 58 * s), 22 * s, color=CAPE, fill=CAPE)
    page.draw_rect(
        fitz.Rect(cx - 16 * s, cy - 24 * s, cx + 16 * s, cy + 36 * s),
        color=CAPE_DK,
        fill=CAPE,
        width=1.2,
    )
    page.draw_rect(
        fitz.Rect(cx - 22 * s, cy - 8 * s, cx - 10 * s, cy + 28 * s),
        color=CAPE_DK,
        fill=CAPE_HI,
        width=1.0,
    )
    page.draw_rect(
        fitz.Rect(cx + 10 * s, cy - 8 * s, cx + 22 * s, cy + 28 * s),
        color=CAPE_DK,
        fill=CAPE_HI,
        width=1.0,
    )


def _draw_wolf(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    s = scale
    body = fitz.Rect(cx - 38 * s, cy - 10 * s, cx + 38 * s, cy + 34 * s)
    page.draw_oval(body, color=WOLF_DK, fill=WOLF, width=1.2)
    head = fitz.Rect(cx + 18 * s, cy - 34 * s, cx + 58 * s, cy + 6 * s)
    page.draw_oval(head, color=WOLF_DK, fill=WOLF, width=1.2)
    page.draw_circle(fitz.Point(cx + 44 * s, cy - 16 * s), 3 * s, color=INK, fill=INK)
    page.draw_line(
        fitz.Point(cx + 58 * s, cy - 8 * s),
        fitz.Point(cx + 72 * s, cy - 2 * s),
        color=WOLF_DK,
        width=2,
    )


def _draw_cottage(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    s = scale
    base = fitz.Rect(cx - 70 * s, cy - 10 * s, cx + 70 * s, cy + 60 * s)
    page.draw_rect(base, color=COTTAGE_DK, fill=COTTAGE, width=1.5)
    roof = fitz.Rect(cx - 82 * s, cy - 52 * s, cx + 82 * s, cy - 8 * s)
    page.draw_rect(roof, color=(0.48, 0.22, 0.16), fill=(0.62, 0.30, 0.20), width=1.2)
    page.draw_rect(
        fitz.Rect(cx - 16 * s, cy + 10 * s, cx + 16 * s, cy + 58 * s),
        color=COTTAGE_DK,
        fill=(0.42, 0.24, 0.16),
        width=1.0,
    )


def _draw_grandmother(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    s = scale
    page.draw_circle(fitz.Point(cx, cy - 36 * s), 20 * s, color=INK, fill=SKIN)
    page.draw_rect(
        fitz.Rect(cx - 24 * s, cy - 18 * s, cx + 24 * s, cy + 40 * s),
        color=(0.72, 0.72, 0.78),
        fill=(0.88, 0.88, 0.92),
        width=1.2,
    )
    for x in (cx - 14 * s, cx, cx + 14 * s):
        page.draw_line(
            fitz.Point(x, cy - 52 * s),
            fitz.Point(x, cy - 18 * s),
            color=HAIR,
            width=3 * s,
        )


def _cover_raster() -> bytes:
    from PIL import Image, ImageDraw

    width, height = 640, 420
    img = Image.new("RGB", (width, height), (236, 228, 206))
    d = ImageDraw.Draw(img)
    d.rectangle([0, height - 120, width, height], fill=(48, 96, 68))
    for x in (80, 200, 420, 540):
        d.polygon([(x, 80), (x - 40, 200), (x + 40, 200)], fill=(52, 118, 78))
        d.rectangle([x - 8, 200, x + 8, 230], fill=(90, 60, 34))
    # Red hood figure.
    d.ellipse([width // 2 - 28, 150, width // 2 + 28, 206], fill=(210, 48, 58))
    d.rectangle([width // 2 - 22, 206, width // 2 + 22, 300], fill=(184, 36, 48))
    d.ellipse([width // 2 - 16, 128, width // 2 + 16, 160], fill=(244, 214, 188))
    # Cottage.
    d.rectangle([width - 200, 220, width - 60, 310], fill=(214, 178, 118))
    d.polygon([(width - 210, 220), (width - 130, 160), (width - 50, 220)], fill=(150, 72, 44))
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
        "1. A basket for Grandmother",
        (
            "Once upon a time there was a sweet little girl whom everyone loved. Her "
            "Grandmother had made her a little red riding hood, and because she wore it "
            "so often, she was called Little Red Riding Hood. One day her mother said, "
            "\u201cTake this cake and bottle of wine to your sick Grandmother. Stay on the "
            "path through the wood, and do not stray.\u201d Little Red Riding Hood promised, "
            "took the basket, and set out through the forest."
        ),
        lambda page, panel: (
            _draw_forest(page, panel),
            _draw_red_riding_hood(page, panel.x0 + 180, panel.y0 + 180, scale=1.1),
        ),
    )

    _story_page(
        doc,
        "2. The Wolf on the path",
        (
            "As she walked, a Wolf met her. He had wicked thoughts, but spoke kindly. "
            "\u201cGood day, Little Red Riding Hood. Where are you going?\u201d \u201cTo my "
            "Grandmother, who is ill.\u201d The Wolf asked where she lived, and she told him. "
            "Then he said, \u201cSee what pretty flowers grow here! Why do you not pick a "
            "nosegay for her?\u201d The child left the path and wandered among the flowers, "
            "while the Wolf ran straight to Grandmother\u2019s cottage."
        ),
        lambda page, panel: (
            _draw_forest(page, panel),
            _draw_red_riding_hood(page, panel.x0 + 120, panel.y0 + 190, scale=1.0),
            _draw_wolf(page, panel.x1 - 150, panel.y0 + 200, scale=1.0),
        ),
    )

    _story_page(
        doc,
        "3. At Grandmother\u2019s door",
        (
            "The Wolf knocked at the cottage door. He disguised his voice and was let in. "
            "Soon after, Little Red Riding Hood arrived with her basket. She was surprised "
            "to find the door standing open. Inside, her Grandmother lay in bed with the "
            "curtains drawn. \u201cOh, Grandmother,\u201d she said, \u201cwhat big ears you have!\u201d "
            "\u201cThe better to hear you with.\u201d \u201cWhat big eyes you have!\u201d \u201cThe better to "
            "see you with.\u201d \u201cWhat big teeth you have!\u201d \u201cThe better to eat you with!\u201d"
        ),
        lambda page, panel: (
            _draw_cottage(page, panel.x0 + 200, panel.y0 + 210, scale=1.2),
            _draw_grandmother(page, panel.x1 - 150, panel.y0 + 180, scale=0.9),
            _draw_wolf(page, panel.x1 - 150, panel.y0 + 180, scale=0.75),
        ),
    )

    _story_page(
        doc,
        "4. Deliverance in the wood",
        (
            "A huntsman passing by heard a great noise in the cottage. He stepped inside, "
            "drew his knife, and rescued Grandmother and Little Red Riding Hood from the "
            "Wolf. They filled the Wolf\u2019s belly with stones; when he woke and tried to "
            "run away, the stones were so heavy that he fell down dead. Little Red Riding "
            "Hood went home, and from that time she kept to the path and minded her "
            "mother\u2019s words."
        ),
        lambda page, panel: (
            _draw_forest(page, panel),
            _draw_red_riding_hood(page, panel.x0 + 160, panel.y0 + 180, scale=1.0),
            _draw_cottage(page, panel.x1 - 170, panel.y0 + 220, scale=1.0),
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
