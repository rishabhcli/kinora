"""Pure cover-sourcing logic — Agent 05 (WS2 HD covers).

Network is mocked out: these exercise the URL builders, the response parsers
(Open Library / Google Books), the deterministic typographic fallback, and the
Retina up-scaler — the parts that must be correct before any byte is fetched.
"""

from __future__ import annotations

import io

from app.library import covers


def test_openlibrary_cover_url_uses_large_size() -> None:
    assert covers.openlibrary_cover_url(12345) == (
        "https://covers.openlibrary.org/b/id/12345-L.jpg"
    )
    assert covers.openlibrary_cover_url(7, size="M").endswith("/7-M.jpg")


def test_openlibrary_search_url_quotes_params() -> None:
    url = covers.openlibrary_search_url("Pride and Prejudice", "Jane Austen")
    assert url.startswith("https://openlibrary.org/search.json?")
    assert "Pride+and+Prejudice" in url or "Pride%20and%20Prejudice" in url
    assert "Austen" in url
    assert "limit=" in url


def test_pick_openlibrary_cover_id_prefers_doc_with_cover() -> None:
    payload = {
        "docs": [
            {"title": "Pride and Prejudice", "author_name": ["Jane Austen"]},  # no cover_i
            {"title": "Pride and Prejudice", "author_name": ["Jane Austen"], "cover_i": 999},
        ]
    }
    assert covers.pick_openlibrary_cover_id(payload) == 999


def test_pick_openlibrary_cover_id_none_when_absent() -> None:
    assert covers.pick_openlibrary_cover_id({"docs": []}) is None
    assert covers.pick_openlibrary_cover_id({}) is None


def test_upgrade_google_cover_url_forces_https_high_res() -> None:
    raw = "http://books.google.com/books/content?id=abc&printsec=frontcover&img=1&zoom=5&edge=curl"
    up = covers.upgrade_google_cover_url(raw)
    assert up.startswith("https://")
    assert "edge=curl" not in up
    assert "zoom=1" in up  # zoom=1 is the largest content image


def test_pick_google_cover_url_picks_best_imagelink() -> None:
    payload = {
        "items": [
            {
                "volumeInfo": {
                    "imageLinks": {
                        "smallThumbnail": "http://x/s",
                        "thumbnail": "http://books.google.com/books/content?id=z&zoom=5&edge=curl",
                    }
                }
            }
        ]
    }
    url = covers.pick_google_cover_url(payload)
    assert url is not None and url.startswith("https://") and "edge=curl" not in url


def test_make_typographic_cover_is_valid_png_of_requested_size() -> None:
    from PIL import Image

    data = covers.make_typographic_cover("A Tale of Two Cities", "Charles Dickens", size=(600, 900))
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(data))
    assert img.size == (600, 900)


def test_make_typographic_cover_palette_is_deterministic() -> None:
    a = covers.make_typographic_cover("Moby Dick", "Herman Melville")
    b = covers.make_typographic_cover("Moby Dick", "Herman Melville")
    assert a == b  # same title/author → identical bytes (stable shelf)
    c = covers.make_typographic_cover("Dracula", "Bram Stoker")
    assert a != c


def test_ensure_min_width_upscales_small_images() -> None:
    from PIL import Image

    small = io.BytesIO()
    Image.new("RGB", (100, 150), (10, 20, 30)).save(small, "PNG")
    out = covers.ensure_min_width(small.getvalue(), 600)
    img = Image.open(io.BytesIO(out))
    assert img.width >= 600
    # aspect ratio preserved (2:3 → height ~ 1.5 * width)
    assert abs(img.height / img.width - 1.5) < 0.02


def test_ensure_min_width_leaves_large_images_untouched() -> None:
    from PIL import Image

    big = io.BytesIO()
    Image.new("RGB", (800, 1200), (10, 20, 30)).save(big, "PNG")
    raw = big.getvalue()
    assert covers.ensure_min_width(raw, 600) == raw
