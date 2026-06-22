#!/usr/bin/env python3
"""Build Kinora's bundled public-domain demo book as a real, multi-page PDF.

Story: **"The Frog-King"** — an abridged retelling of the Brothers Grimm fairy
tale (*Der Froschkönig*, Kinder- und Hausmärchen, 1812). The tale and its 19th
century English translations are firmly in the **public domain**, so it is safe
to ship in a public, recorded submission with zero copyright risk (kinora.md
§15: "Short, few characters, strong visuals, and public domain"). The wording
here is a short, faithful abridgement of the public-domain translation.

The book is deliberately small and demo-friendly:

* a title page + four story pages (five total),
* exactly three characters the ingest canon will lock — the **Princess**, the
  **Frog** (who becomes the **Prince**), and the **King**,
* a real, selectable text layer on every page (so PyMuPDF extraction yields the
  per-word boxes the karaoke / source-span index needs), and
* a simple illustration on every page: vector art drawn with PyMuPDF, plus one
  embedded raster cover scene (Pillow) so the page also carries a bitmap image.

Run it with the backend venv (PyMuPDF + Pillow are backend deps)::

    backend/.venv/bin/python assets/books/build_demo_pdf.py

It writes ``assets/books/the_frog_king.pdf`` next to this script and then
re-opens it to assert the book is a valid multi-page PDF with text, drawn
vector illustrations, and an embedded image.
"""

from __future__ import annotations

import io
from pathlib import Path

import fitz  # PyMuPDF

HERE = Path(__file__).resolve().parent
OUT_PATH = HERE / "the_frog_king.pdf"

# Storybook page geometry (portrait, a touch taller than Letter).
PAGE_W, PAGE_H = 720.0, 960.0
MARGIN = 64.0

# Palette (RGB 0..1) for the painterly-storybook art direction.
GOLD = (0.86, 0.68, 0.18)
GOLD_HI = (0.98, 0.88, 0.45)
FROG = (0.25, 0.55, 0.27)
FROG_DK = (0.16, 0.38, 0.19)
STONE = (0.55, 0.50, 0.44)
STONE_DK = (0.40, 0.36, 0.31)
WATER = (0.12, 0.29, 0.42)
INK = (0.13, 0.12, 0.16)
PARCHMENT = (0.97, 0.95, 0.89)
FOREST = (0.20, 0.42, 0.30)
ROBE = (0.62, 0.16, 0.22)

TITLE_FONT = "times-bold"
BODY_FONT = "times-roman"
ITALIC_FONT = "times-italic"


# --------------------------------------------------------------------------- #
# Vector illustration helpers (real drawn art, kept tiny + crisp)
# --------------------------------------------------------------------------- #


def _fill_background(page: fitz.Page, color: tuple[float, float, float]) -> None:
    page.draw_rect(page.rect, color=color, fill=color)


def _draw_well(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    """A round stone well with dark water — the scene's anchor location."""
    w = 150 * scale
    h = 60 * scale
    body = fitz.Rect(cx - w / 2, cy - h, cx + w / 2, cy + h * 1.4)
    page.draw_rect(body, color=STONE_DK, fill=STONE, width=1.5)
    # Brick courses.
    for i in range(1, 4):
        y = body.y0 + (body.height * i / 4)
        page.draw_line(fitz.Point(body.x0, y), fitz.Point(body.x1, y), color=STONE_DK, width=1)
    # Water mouth.
    mouth = fitz.Rect(cx - w / 2, cy - h - 10 * scale, cx + w / 2, cy - h + 14 * scale)
    page.draw_oval(mouth, color=STONE_DK, fill=WATER, width=1.5)
    # Little roof posts + beam.
    page.draw_line(
        fitz.Point(cx - w / 2, cy - h - 8 * scale),
        fitz.Point(cx - w / 2, cy - h - 70 * scale),
        color=STONE_DK,
        width=4,
    )
    page.draw_line(
        fitz.Point(cx + w / 2, cy - h - 8 * scale),
        fitz.Point(cx + w / 2, cy - h - 70 * scale),
        color=STONE_DK,
        width=4,
    )
    page.draw_line(
        fitz.Point(cx - w / 2 - 12 * scale, cy - h - 66 * scale),
        fitz.Point(cx + w / 2 + 12 * scale, cy - h - 66 * scale),
        color=STONE_DK,
        width=5,
    )


def _draw_golden_ball(page: fitz.Page, cx: float, cy: float, r: float = 22.0) -> None:
    """The princess's golden ball with a soft highlight."""
    page.draw_circle(fitz.Point(cx, cy), r, color=(0.6, 0.45, 0.1), fill=GOLD, width=1.5)
    page.draw_circle(fitz.Point(cx - r * 0.3, cy - r * 0.3), r * 0.35, color=None, fill=GOLD_HI)


def _draw_frog(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    """A friendly frog: body, two eyes, a small smile."""
    body = fitz.Rect(cx - 46 * scale, cy - 30 * scale, cx + 46 * scale, cy + 34 * scale)
    page.draw_oval(body, color=FROG_DK, fill=FROG, width=1.5)
    for dx in (-22 * scale, 22 * scale):
        eye = fitz.Rect(cx + dx - 16 * scale, cy - 52 * scale, cx + dx + 16 * scale, cy - 22 * scale)
        page.draw_oval(eye, color=FROG_DK, fill=FROG, width=1.5)
        page.draw_circle(fitz.Point(cx + dx, cy - 37 * scale), 6 * scale, color=None, fill=(1, 1, 1))
        page.draw_circle(fitz.Point(cx + dx, cy - 37 * scale), 3 * scale, color=None, fill=INK)
    page.draw_bezier(
        fitz.Point(cx - 20 * scale, cy + 6 * scale),
        fitz.Point(cx - 8 * scale, cy + 18 * scale),
        fitz.Point(cx + 8 * scale, cy + 18 * scale),
        fitz.Point(cx + 20 * scale, cy + 6 * scale),
        color=FROG_DK,
        width=2,
    )
    # Front legs.
    for dx in (-40 * scale, 40 * scale):
        page.draw_line(
            fitz.Point(cx + dx, cy + 28 * scale),
            fitz.Point(cx + dx * 1.25, cy + 46 * scale),
            color=FROG_DK,
            width=4 * scale,
        )


def _draw_crown(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    """A small golden crown (the King / the prince's royalty)."""
    w = 70 * scale
    h = 40 * scale
    pts = [
        fitz.Point(cx - w / 2, cy + h / 2),
        fitz.Point(cx - w / 2, cy - h / 4),
        fitz.Point(cx - w / 4, cy + h / 8),
        fitz.Point(cx, cy - h / 2),
        fitz.Point(cx + w / 4, cy + h / 8),
        fitz.Point(cx + w / 2, cy - h / 4),
        fitz.Point(cx + w / 2, cy + h / 2),
    ]
    page.draw_polyline(pts, color=(0.6, 0.45, 0.1), fill=GOLD, width=1.5, closePath=True)
    for dx in (-w / 4, 0, w / 4):
        page.draw_circle(fitz.Point(cx + dx, cy - h / 4), 4 * scale, color=None, fill=ROBE)


def _draw_princess(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    """A simple princess figure: gown triangle, head, small crown."""
    gown = [
        fitz.Point(cx, cy - 20 * scale),
        fitz.Point(cx - 40 * scale, cy + 70 * scale),
        fitz.Point(cx + 40 * scale, cy + 70 * scale),
    ]
    page.draw_polyline(gown, color=(0.4, 0.1, 0.14), fill=ROBE, width=1.5, closePath=True)
    page.draw_circle(fitz.Point(cx, cy - 34 * scale), 16 * scale, color=(0.5, 0.36, 0.26), fill=(0.97, 0.82, 0.66), width=1.2)
    _draw_crown(page, cx, cy - 52 * scale, scale=0.5)


def _draw_table(page: fitz.Page, cx: float, cy: float, scale: float = 1.0) -> None:
    """A little supper table with a golden plate (the castle hall scene)."""
    top = fitz.Rect(cx - 90 * scale, cy - 10 * scale, cx + 90 * scale, cy + 4 * scale)
    page.draw_rect(top, color=(0.35, 0.24, 0.14), fill=(0.55, 0.38, 0.22))
    for dx in (-78 * scale, 78 * scale):
        page.draw_line(
            fitz.Point(cx + dx, cy + 4 * scale),
            fitz.Point(cx + dx, cy + 60 * scale),
            color=(0.35, 0.24, 0.14),
            width=6 * scale,
        )
    page.draw_oval(
        fitz.Rect(cx - 26 * scale, cy - 22 * scale, cx + 26 * scale, cy - 6 * scale),
        color=(0.6, 0.45, 0.1),
        fill=GOLD,
        width=1.2,
    )


def _cover_raster(width: int = 520, height: int = 300) -> bytes:
    """Render an embedded raster cover scene with Pillow (a real bitmap image).

    Drawn programmatically (no external asset) so the committed PDF stays tiny
    while still carrying a genuine embedded image alongside the vector art.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (231, 224, 196))
    d = ImageDraw.Draw(img)
    # Sky + forest floor.
    d.rectangle([0, 0, width, int(height * 0.62)], fill=(206, 224, 213))
    d.rectangle([0, int(height * 0.62), width, height], fill=(120, 150, 96))
    # A few pines.
    for x in (60, 150, width - 90):
        d.polygon([(x, 70), (x - 34, 190), (x + 34, 190)], fill=(40, 92, 60))
        d.rectangle([x - 6, 190, x + 6, 210], fill=(80, 55, 30))
    # The well.
    wx, wy = int(width * 0.66), int(height * 0.66)
    d.rectangle([wx - 70, wy - 30, wx + 70, wy + 70], fill=(140, 128, 112), outline=(90, 82, 70), width=3)
    d.ellipse([wx - 70, wy - 46, wx + 70, wy - 14], fill=(30, 74, 107), outline=(90, 82, 70), width=3)
    # The frog on the rim.
    d.ellipse([wx - 24, wy - 60, wx + 24, wy - 24], fill=(64, 140, 70), outline=(40, 96, 48), width=2)
    for ex in (wx - 12, wx + 12):
        d.ellipse([ex - 7, wy - 70, ex + 7, wy - 56], fill=(64, 140, 70), outline=(40, 96, 48))
        d.ellipse([ex - 3, wy - 66, ex + 3, wy - 60], fill=(20, 20, 24))
    # The golden ball, mid-air.
    bx, by = int(width * 0.30), int(height * 0.52)
    d.ellipse([bx - 26, by - 26, bx + 26, by + 26], fill=(219, 174, 46), outline=(150, 112, 26), width=2)
    d.ellipse([bx - 16, by - 16, bx - 4, by - 4], fill=(250, 224, 115))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Page text helpers
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# The book
# --------------------------------------------------------------------------- #


def _title_page(doc: fitz.Document) -> None:
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    _fill_background(page, PARCHMENT)
    page.insert_textbox(
        fitz.Rect(MARGIN, 120, PAGE_W - MARGIN, 230),
        "The Frog-King",
        fontname=TITLE_FONT,
        fontsize=46,
        color=INK,
        align=fitz.TEXT_ALIGN_CENTER,
    )
    page.insert_textbox(
        fitz.Rect(MARGIN, 232, PAGE_W - MARGIN, 300),
        "An abridged retelling of the Brothers Grimm fairy tale",
        fontname=ITALIC_FONT,
        fontsize=17,
        color=STONE_DK,
        align=fitz.TEXT_ALIGN_CENTER,
    )
    # Embedded raster cover scene.
    img_rect = fitz.Rect(MARGIN + 30, 330, PAGE_W - MARGIN - 30, 640)
    page.insert_image(img_rect, stream=_cover_raster(), keep_proportion=True)
    page.insert_textbox(
        fitz.Rect(MARGIN, 720, PAGE_W - MARGIN, 800),
        "Public domain \u00b7 a Kinora demo book \u2014 watch the book.",
        fontname=BODY_FONT,
        fontsize=13,
        color=STONE_DK,
        align=fitz.TEXT_ALIGN_CENTER,
    )


def _story_page(
    doc: fitz.Document,
    heading: str,
    body: str,
    illustrate,
) -> None:
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    _fill_background(page, PARCHMENT)
    _heading(page, heading, MARGIN)
    _paragraph(page, body, MARGIN + 56, height=300)
    # Illustration panel in the lower half.
    panel = fitz.Rect(MARGIN, 470, PAGE_W - MARGIN, 860)
    page.draw_rect(panel, color=(0.80, 0.76, 0.66), fill=(0.93, 0.91, 0.84), width=1.2)
    illustrate(page, panel)


def build() -> Path:
    """Build the demo PDF and return its path."""
    doc = fitz.open()
    doc.set_metadata(
        {
            "title": "The Frog-King (abridged)",
            "author": "Brothers Grimm (public domain) \u2014 abridged for Kinora",
            "subject": "Public-domain demo book for Kinora generation-on-scroll",
            "keywords": "public-domain, Grimm, fairy tale, Kinora demo",
        }
    )

    _title_page(doc)

    _story_page(
        doc,
        "1. The golden ball",
        (
            "In the old time, when wishing still helped, there lived a King whose "
            "youngest daughter was so beautiful that the sun itself was astonished "
            "whenever it shone upon her face. Near the King's castle lay a great, dark "
            "forest, and in the forest, beneath an old lime-tree, there was a deep well. "
            "When the day was hot, the young Princess went out into the forest and sat by "
            "the cool well; and when she was bored she took a golden ball, threw it up on "
            "high, and caught it again; and this was her favourite plaything."
        ),
        lambda page, panel: (
            _draw_princess(page, panel.x0 + 130, panel.y0 + 150, scale=1.2),
            _draw_golden_ball(page, panel.x0 + 250, panel.y0 + 90, r=20),
            _draw_well(page, panel.x1 - 150, panel.y0 + 210, scale=1.0),
        ),
    )

    _story_page(
        doc,
        "2. A promise at the well",
        (
            "One day the golden ball did not fall into her little hand, but slipped past "
            "and rolled straight into the deep water. The Princess wept, and could not be "
            "comforted. Then a voice cried to her: \u201cWhat ails you, King\u2019s daughter?\u201d "
            "She looked round, and saw a Frog stretching his thick, ugly head from the "
            "water. \u201cI weep for my golden ball, which has fallen into the well.\u201d The "
            "Frog said, \u201cIf you will love me, and let me be your companion, and eat from "
            "your little golden plate, I will bring your ball again.\u201d She promised, and "
            "the Frog dived down and brought the golden ball up in his mouth."
        ),
        lambda page, panel: (
            _draw_well(page, panel.x0 + 170, panel.y0 + 200, scale=1.2),
            _draw_frog(page, panel.x0 + 170, panel.y0 + 120, scale=1.0),
            _draw_golden_ball(page, panel.x1 - 150, panel.y0 + 150, r=22),
        ),
    )

    _story_page(
        doc,
        "3. A knock at the castle door",
        (
            "The next day, as the Princess sat at dinner with the King and all the court, "
            "something came creeping up the marble stairs, and knocked at the door, "
            "crying, \u201cPrincess, youngest Princess, open the door for me!\u201d It was the "
            "Frog. The King saw that she was troubled, and asked what she feared. When she "
            "told him of her promise at the well, the King said, \u201cThat which you have "
            "promised must you perform. Go and let him in.\u201d So the Frog was set upon the "
            "table, and ate from her little golden plate beside her."
        ),
        lambda page, panel: (
            _draw_crown(page, panel.x0 + 110, panel.y0 + 80, scale=1.1),
            _draw_table(page, panel.x0 + 270, panel.y0 + 220, scale=1.2),
            _draw_frog(page, panel.x0 + 270, panel.y0 + 150, scale=0.7),
        ),
    )

    _story_page(
        doc,
        "4. The spell is broken",
        (
            "When the Frog had eaten, he asked to rest, and the Princess carried him up to "
            "her room. As he lay upon her soft pillow through the night, the enchantment "
            "fell away. In the morning, where the Frog had been, there stood a young Prince "
            "with kind and beautiful eyes. He told her that a wicked witch had bewitched "
            "him, and that she alone could have set him free. And so the Princess and the "
            "Prince became dear and faithful friends, and there was great joy throughout "
            "the King\u2019s castle."
        ),
        lambda page, panel: (
            _draw_princess(page, panel.x0 + 140, panel.y0 + 160, scale=1.1),
            _draw_crown(page, panel.x1 - 170, panel.y0 + 70, scale=1.0),
            _draw_golden_ball(page, panel.x1 - 170, panel.y0 + 160, r=18),
        ),
    )

    doc.save(str(OUT_PATH), deflate=True, garbage=4)
    doc.close()
    return OUT_PATH


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


def verify(path: Path) -> dict[str, object]:
    """Re-open the PDF and assert it is a valid, multi-page illustrated book."""
    doc = fitz.open(str(path))
    try:
        num_pages = doc.page_count
        total_words = 0
        total_drawings = 0
        total_images = 0
        for page in doc:
            total_words += len(page.get_text("words"))
            total_drawings += len(page.get_drawings())
            total_images += len(page.get_images(full=True))
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
