"""High-definition cover sourcing + a tasteful generated fallback (Agent 05, WS2).

The shelf renders covers at ~150px but on Retina that wants 2–3× pixels, so we
prefer the highest-resolution source available and never ship a blank cover:

1. **Open Library Covers** — search by title/author → ``cover_i`` → the ``-L``
   (largest) image. Best hit-rate for canonical classics.
2. **Google Books** — ``imageLinks`` upgraded to the full ``zoom=1`` content
   image over https (the ``edge=curl`` page-curl removed).
3. **Generated** — a deterministic typographic cover (serif title + author over a
   per-title gradient) so a text-only Gutenberg EPUB still looks designed.

The URL builders, response parsers, the generator, and the Retina up-scaler are
pure (unit-tested); only :func:`http_get_json` / :func:`http_get_bytes` touch the
network and are used by ``scripts/fetch_hd_covers.py``.
"""

from __future__ import annotations

import colorsys
import hashlib
import io
import re
from urllib.parse import urlencode

#: Minimum cover width we consider "HD" for a Retina shelf (≈150px @ 3×, padded).
HD_MIN_WIDTH = 600
#: Default generated-cover canvas — a 2:3 book ratio at HD.
COVER_SIZE = (720, 1080)

_RGB = tuple[int, int, int]
_CREAM: _RGB = (242, 234, 216)

_FONT_PATHS = (
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/System/Library/Fonts/Supplemental/Baskerville.ttc",
    "/System/Library/Fonts/Supplemental/Hoefler Text.ttc",
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "/Library/Fonts/Georgia.ttf",
)


# --------------------------------------------------------------------------- #
# Open Library
# --------------------------------------------------------------------------- #


def openlibrary_cover_url(cover_id: int, size: str = "L") -> str:
    """The Open Library Covers API URL for ``cover_id`` (``L`` = largest)."""
    return f"https://covers.openlibrary.org/b/id/{cover_id}-{size}.jpg"


def openlibrary_search_url(title: str, author: str | None = None, *, limit: int = 5) -> str:
    """Open Library search URL restricted to the fields we need (small payload)."""
    params: dict[str, str | int] = {
        "title": title,
        "limit": limit,
        "fields": "cover_i,title,author_name,edition_count",
    }
    if author:
        params["author"] = author
    return "https://openlibrary.org/search.json?" + urlencode(params)


def pick_openlibrary_cover_id(payload: dict) -> int | None:
    """First search doc that actually carries a ``cover_i`` (else ``None``)."""
    docs = payload.get("docs") if isinstance(payload, dict) else None
    if not isinstance(docs, list):
        return None
    for doc in docs:
        if isinstance(doc, dict):
            cover_i = doc.get("cover_i")
            if isinstance(cover_i, int) and cover_i > 0:
                return cover_i
    return None


# --------------------------------------------------------------------------- #
# Google Books
# --------------------------------------------------------------------------- #


def google_books_search_url(title: str, author: str | None = None) -> str:
    """Google Books volumes query for the title (+author), one good hit."""
    q = f'intitle:"{title}"'
    if author:
        q += f' inauthor:"{author}"'
    params = {"q": q, "maxResults": 3, "country": "US"}
    return "https://www.googleapis.com/books/v1/volumes?" + urlencode(params)


def upgrade_google_cover_url(raw: str) -> str:
    """Force https, drop the page-curl, and request the full-size content image."""
    url = raw.replace("http://", "https://")
    url = re.sub(r"&?edge=curl", "", url)
    if "zoom=" in url:
        url = re.sub(r"zoom=\d+", "zoom=1", url)
    else:
        url += ("&" if "?" in url else "?") + "zoom=1"
    return url


def pick_google_cover_url(payload: dict) -> str | None:
    """Best ``imageLinks`` from the first volume, upgraded to full-size https."""
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None
    for item in items:
        links = (item.get("volumeInfo") or {}).get("imageLinks") if isinstance(item, dict) else None
        if not isinstance(links, dict):
            continue
        for key in ("extraLarge", "large", "medium", "small", "thumbnail", "smallThumbnail"):
            url = links.get(key)
            if isinstance(url, str) and url:
                return upgrade_google_cover_url(url)
    return None


# --------------------------------------------------------------------------- #
# Generated typographic fallback
# --------------------------------------------------------------------------- #


def _hsv(h: float, s: float, v: float) -> _RGB:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))


def cover_palette(key: str) -> tuple[_RGB, _RGB, _RGB]:
    """Deterministic ``(top, bottom, accent)`` gradient palette for a title.

    Doubles as the STRETCH "per-book accent palette" (Agent 8 theming can read it).
    """
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    hue = digest[0] / 255.0
    top = _hsv(hue, 0.42, 0.40)
    bottom = _hsv(hue + 0.04, 0.55, 0.10)
    accent = _hsv(hue + 0.5, 0.55, 0.82)
    return top, bottom, accent


def _font(size: int):  # type: ignore[no-untyped-def]
    from PIL import ImageFont

    for path in _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: float) -> list[str]:  # type: ignore[no-untyped-def]
    lines: list[str] = []
    cur = ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if not cur or draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def make_typographic_cover(
    title: str, author: str | None = None, *, size: tuple[int, int] = COVER_SIZE
) -> bytes:
    """A clean designed cover (per-title gradient + serif title/author) as PNG bytes.

    Deterministic for a given ``(title, author, size)`` so the shelf is stable.
    """
    from PIL import Image, ImageDraw

    width, height = size
    top, bottom, accent = cover_palette(f"{title}|{author or ''}")
    img = Image.new("RGB", size, bottom)
    draw = ImageDraw.Draw(img, "RGBA")
    for y in range(height):  # vertical gradient, accent-weighted to the upper third
        f = (y / max(1, height - 1)) ** 1.15
        draw.line(
            [(0, y), (width, y)],
            fill=(
                int(top[0] + (bottom[0] - top[0]) * f),
                int(top[1] + (bottom[1] - top[1]) * f),
                int(top[2] + (bottom[2] - top[2]) * f),
            ),
        )
    margin = max(28, width // 16)
    draw.rectangle(
        [margin, margin, width - margin, height - margin], outline=(*accent, 110), width=2
    )

    title_font = _font(int(height * 0.072))
    body_w = width - 2 * margin - int(width * 0.08)
    lines = _wrap(draw, title, title_font, body_w)
    while len(lines) > 4 and title_font.size > int(height * 0.04):
        title_font = _font(title_font.size - 6)
        lines = _wrap(draw, title, title_font, body_w)
    y = int(height * 0.30)
    for line in lines:
        w = draw.textlength(line, font=title_font)
        draw.text(((width - w) / 2, y), line, font=title_font, fill=_CREAM)
        y += int(title_font.size * 1.16)
    if author:
        rule_y = y + int(height * 0.022)
        draw.line(
            [(width / 2 - 70, rule_y), (width / 2 + 70, rule_y)], fill=accent, width=2
        )
        author_font = _font(int(height * 0.033))
        aw = draw.textlength(author, font=author_font)
        draw.text(
            ((width - aw) / 2, rule_y + int(height * 0.026)),
            author,
            font=author_font,
            fill=(*_CREAM, 220),
        )

    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


# --------------------------------------------------------------------------- #
# Retina up-scaler
# --------------------------------------------------------------------------- #


def ensure_min_width(image_bytes: bytes, min_width: int = HD_MIN_WIDTH) -> bytes:
    """Up-scale an image (preserving aspect) to at least ``min_width`` px wide.

    Returns the input unchanged when it is already wide enough (so a real HD cover
    is never re-encoded / degraded).
    """
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes))
    if img.width >= min_width:
        return image_bytes
    scale = min_width / img.width
    resized = img.convert("RGB").resize(
        (min_width, max(1, round(img.height * scale))), Image.Resampling.LANCZOS
    )
    out = io.BytesIO()
    resized.save(out, "PNG")
    return out.getvalue()


# --------------------------------------------------------------------------- #
# Network helpers (used by scripts/fetch_hd_covers.py only)
# --------------------------------------------------------------------------- #


def http_get_json(url: str, *, timeout: float = 20.0) -> dict | None:
    """GET ``url`` and parse JSON, returning ``None`` on any failure (best-effort)."""
    import httpx

    try:
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            resp = client.get(url, headers={"User-Agent": "Kinora/2.0 (+library)"})
            resp.raise_for_status()
            data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - best-effort cover sourcing, never fatal
        return None


def http_get_bytes(url: str, *, timeout: float = 30.0, min_bytes: int = 1500) -> bytes | None:
    """GET ``url`` as image bytes, rejecting tiny/empty bodies (best-effort)."""
    import httpx

    try:
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            resp = client.get(url, headers={"User-Agent": "Kinora/2.0 (+library)"})
            resp.raise_for_status()
            data = resp.content
        return data if len(data) >= min_bytes else None
    except Exception:  # noqa: BLE001 - best-effort cover sourcing, never fatal
        return None


def source_hd_cover(title: str, author: str | None) -> tuple[bytes, str] | None:
    """Best available HD cover bytes + its source label, or ``None`` (network).

    Tries Open Library (``-L``) then Google Books (full-size), up-scaling to
    :data:`HD_MIN_WIDTH`. The caller supplies the generated fallback when this
    returns ``None``, so a cover is never blank.
    """
    ol = http_get_json(openlibrary_search_url(title, author))
    if ol is not None:
        cover_id = pick_openlibrary_cover_id(ol)
        if cover_id is not None:
            data = http_get_bytes(openlibrary_cover_url(cover_id))
            if data is not None:
                return ensure_min_width(data), "openlibrary"
    gb = http_get_json(google_books_search_url(title, author))
    if gb is not None:
        url = pick_google_cover_url(gb)
        if url is not None:
            data = http_get_bytes(url)
            if data is not None:
                return ensure_min_width(data), "google"
    return None


__all__ = [
    "COVER_SIZE",
    "HD_MIN_WIDTH",
    "cover_palette",
    "source_hd_cover",
    "ensure_min_width",
    "google_books_search_url",
    "http_get_bytes",
    "http_get_json",
    "make_typographic_cover",
    "openlibrary_cover_url",
    "openlibrary_search_url",
    "pick_google_cover_url",
    "pick_openlibrary_cover_id",
    "upgrade_google_cover_url",
]
