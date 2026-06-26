"""Curated public-domain catalogue — pure logic (Agent 05, WS1).

Genre/era classification, stable id derivation, dedupe, and catalog.json
round-trip — the deterministic spine the zero-spend seeder relies on.
"""

from __future__ import annotations

from pathlib import Path

from app.library import catalog
from app.library.catalog import CatalogEntry


def test_book_id_for_is_stable_and_32_chars() -> None:
    bid = catalog.book_id_for(1342)
    assert bid.startswith("pubdom1342")
    assert len(bid) == 32
    assert catalog.book_id_for(1342) == bid  # stable across calls


def test_genre_for_maps_subjects() -> None:
    assert catalog.genre_for(["Science fiction"], []) == "Science Fiction"
    assert catalog.genre_for(["Horror tales", "Fiction"], []) == "Gothic & Horror"
    assert catalog.genre_for(["Detective and mystery stories"], []) == "Mystery & Detective"
    assert catalog.genre_for(["Adventure stories"], []) == "Adventure"
    assert catalog.genre_for(["Love stories"], []) == "Romance"
    assert catalog.genre_for(["Fairy tales"], ["Children's Literature"]) == "Children's"
    assert catalog.genre_for(["Poetry"], []) == "Poetry"
    assert catalog.genre_for(["Fiction"], []) == "Classics"
    assert catalog.genre_for([], []) == "Classics"


def test_era_for_buckets_by_year() -> None:
    assert catalog.era_for(1775, 1817) == "19th century"
    assert catalog.era_for(1812, 1870) == "19th century"
    assert catalog.era_for(None, 1950) == "20th century"
    assert catalog.era_for(-800, -700) == "Antiquity"
    assert catalog.era_for(None, None) == "Classic"


def test_dedupe_by_gid_keeps_first() -> None:
    a = CatalogEntry(gutenberg_id=11, title="Alice", author="Carroll")
    b = CatalogEntry(gutenberg_id=11, title="Alice (dup)", author="Carroll")
    c = CatalogEntry(gutenberg_id=84, title="Frankenstein", author="Shelley")
    out = catalog.dedupe_by_gid([a, b, c])
    assert [e.gutenberg_id for e in out] == [11, 84]
    assert out[0].title == "Alice"


def test_catalog_entry_roundtrips() -> None:
    e = CatalogEntry(
        gutenberg_id=1342,
        title="Pride and Prejudice",
        author="Jane Austen",
        genre="Romance",
        era="19th century",
        tags=["romance", "classic"],
    )
    restored = CatalogEntry.from_dict(e.to_dict())
    assert restored == e
    assert restored.id == catalog.book_id_for(1342)


def test_write_and_load_catalog_roundtrip(tmp_path: Path) -> None:
    entries = [
        CatalogEntry(gutenberg_id=11, title="Alice", author="Carroll", genre="Children's"),
        CatalogEntry(
            gutenberg_id=84, title="Frankenstein", author="Shelley", genre="Gothic & Horror"
        ),
    ]
    path = tmp_path / "catalog.json"
    catalog.write_catalog(entries, path)
    loaded = catalog.load_catalog(path)
    assert [e.gutenberg_id for e in loaded] == [11, 84]
    assert loaded[0].genre == "Children's"


def test_gutenberg_epub_url_uses_cache_mirror() -> None:
    assert catalog.gutenberg_epub_url(1342) == (
        "https://www.gutenberg.org/cache/epub/1342/pg1342.epub"
    )
