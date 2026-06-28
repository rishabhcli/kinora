"""Unit tests for row serialization + id remapping + key rewriting (no infra)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from app.dataportability.errors import ReferentialIntegrityError
from app.dataportability.idremap import IdRemapper
from app.dataportability.keys import (
    collect_book_keys,
    deterministic_book_keys,
    rewrite_key_book_id,
)
from app.dataportability.serialization import (
    BOOK_SCOPED_TABLES,
    CANON_TABLES,
    RowCodec,
    row_codec_for,
    table_registry,
)
from app.db.models.entity import Entity
from app.db.models.enums import EntityType
from app.db.models.shot import Shot

# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #


def test_entity_round_trips_through_codec() -> None:
    codec = RowCodec(Entity)
    now = dt.datetime(2026, 6, 28, 12, 0, tzinfo=dt.UTC)
    entity = Entity(
        id="e1",
        book_id="b1",
        entity_key="char_elsa",
        type=EntityType.CHARACTER,
        name="Elsa",
        aliases=["the Snow Queen"],
        description="platinum braid",
        appearance={"description": "ice gown", "reference_image_keys": ["refs/b1/char_elsa/f.png"]},
        embedding=[0.0] * 1152,
        version=3,
        valid_from_beat=1,
        valid_to_beat=None,
        created_at=now,
        updated_at=now,
    )
    data = codec.to_dict(entity)
    assert data["entity_key"] == "char_elsa"
    assert data["type"] == "character"  # enum -> value
    assert data["created_at"] == now.isoformat()  # datetime -> iso
    assert isinstance(data["embedding"], list) and len(data["embedding"]) == 1152
    # keys are sorted/deterministic
    assert list(data.keys()) == sorted(data.keys())

    kwargs = codec.from_dict(data)
    assert kwargs["entity_key"] == "char_elsa"
    assert isinstance(kwargs["created_at"], dt.datetime)
    assert kwargs["created_at"].tzinfo is not None
    rebuilt = Entity(**kwargs)
    assert rebuilt.name == "Elsa"
    assert rebuilt.embedding == [0.0] * 1152


def test_codec_is_column_complete() -> None:
    codec = RowCodec(Shot)
    mapped = {c.key for c in Shot.__mapper__.columns}
    assert set(codec.columns) == mapped


def test_from_dict_drops_unknown_and_tolerates_missing() -> None:
    codec = RowCodec(Shot)
    kwargs = codec.from_dict({"id": "s1", "book_id": "b1", "ghost_column": 1})
    assert "ghost_column" not in kwargs
    assert kwargs["id"] == "s1"
    # a column absent from the dict simply doesn't appear (uses default/NULL)
    assert "prompt" not in kwargs


def test_table_registry_covers_book_and_canon_tables() -> None:
    reg = table_registry()
    for t in BOOK_SCOPED_TABLES:
        assert t in reg, t
    for t in CANON_TABLES:
        assert t in reg, t
    # users + books lead the import order (parents before children)
    order = list(reg.keys())
    assert order.index("users") < order.index("books")
    assert order.index("books") < order.index("shots")
    assert order.index("scenes") < order.index("beats")


def test_row_codec_for_builds_by_name() -> None:
    assert row_codec_for("shots").model is Shot


# --------------------------------------------------------------------------- #
# Id remapping
# --------------------------------------------------------------------------- #


def test_remap_mints_fresh_pk_and_rewrites_fk() -> None:
    remap = IdRemapper()
    books = [{"id": "oldbook", "user_id": "u1", "title": "T"}]
    shots = [{"id": "s1", "book_id": "oldbook", "scene_id": "scene_005"}]
    remap.force_user_id("u1", "u1new")
    remap.mint_table("books", books)
    remap.mint_table("shots", shots)

    new_book = remap.rewrite_row("books", books[0])
    new_shot = remap.rewrite_row("shots", shots[0])

    assert new_book["id"] != "oldbook"
    assert new_book["user_id"] == "u1new"
    # the shot's book_id resolves to the SAME new id as the book's PK
    assert new_shot["book_id"] == new_book["id"]
    # scene_id is a book-stable canon string, NOT remapped
    assert new_shot["scene_id"] == "scene_005"


def test_remap_dangling_reference_fails_closed() -> None:
    remap = IdRemapper()
    # a shot referencing a book id that was never minted
    shots = [{"id": "s1", "book_id": "ghost"}]
    remap.mint_table("shots", shots)
    with pytest.raises(ReferentialIntegrityError) as ei:
        remap.rewrite_row("shots", shots[0])
    assert ei.value.column == "book_id"
    assert ei.value.value == "ghost"


def test_remap_supersedes_resolves_within_entities() -> None:
    remap = IdRemapper()
    entities: list[dict[str, Any]] = [
        {"id": "e1", "book_id": "b1", "entity_key": "char_x", "supersedes": None},
        {"id": "e2", "book_id": "b1", "entity_key": "char_x", "supersedes": "e1"},
    ]
    remap.mint_table("books", [{"id": "b1"}])
    remap.mint_table("entities", entities)
    out2 = remap.rewrite_row("entities", entities[1])
    out1 = remap.rewrite_row("entities", entities[0])
    assert out2["supersedes"] == out1["id"]


def test_remap_budget_reservation_id_follows_pk() -> None:
    remap = IdRemapper()
    rows = [
        {"id": "r1", "book_id": "b1", "kind": "reserve", "reservation_id": "r1"},
        {"id": "r2", "book_id": "b1", "kind": "commit", "reservation_id": "r1"},
    ]
    remap.mint_table("books", [{"id": "b1"}])
    remap.mint_table("budget_ledger", rows)
    out1 = remap.rewrite_row("budget_ledger", rows[0])
    out2 = remap.rewrite_row("budget_ledger", rows[1])
    # reserve row points at itself; commit points at the reserve row
    assert out1["reservation_id"] == out1["id"]
    assert out2["reservation_id"] == out1["id"]


def test_remap_shot_cache_keeps_content_hash_pk() -> None:
    remap = IdRemapper()
    rows = [{"shot_hash": "sha1:abc", "book_id": "b1", "clip_key": "clips/b1/s.mp4"}]
    remap.mint_table("books", [{"id": "b1"}])
    remap.mint_table("shot_cache", rows)  # no-op: no remappable PK
    out = remap.rewrite_row("shot_cache", rows[0])
    assert out["shot_hash"] == "sha1:abc"  # cache hash preserved (re-read hits)
    assert out["book_id"] != "b1"  # but book_id is remapped


# --------------------------------------------------------------------------- #
# Key rewriting
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "key,expected",
    [
        ("pdfs/OLD.pdf", "pdfs/NEW.pdf"),
        ("epubs/OLD.epub", "epubs/NEW.epub"),
        ("covers/OLD", "covers/NEW"),
        ("pages/OLD/0001.png", "pages/NEW/0001.png"),
        ("clips/OLD/shot_42.mp4", "clips/NEW/shot_42.mp4"),
        ("refs/OLD/char_elsa/ref_front.png", "refs/NEW/char_elsa/ref_front.png"),
        ("canon/OLD/index.md", "canon/NEW/index.md"),
        ("audio/OLD/shot_1.wav", "audio/NEW/shot_1.wav"),
        ("lastframes/OLD/shot_1.png", "lastframes/NEW/shot_1.png"),
    ],
)
def test_rewrite_key_book_id(key: str, expected: str) -> None:
    assert rewrite_key_book_id(key, "OLD", "NEW") == expected


def test_rewrite_key_leaves_other_books_untouched() -> None:
    # a key embedding a DIFFERENT book id is not rewritten
    assert rewrite_key_book_id("clips/OTHER/s.mp4", "OLD", "NEW") == "clips/OTHER/s.mp4"
    assert rewrite_key_book_id("pdfs/OTHER.pdf", "OLD", "NEW") == "pdfs/OTHER.pdf"


def test_deterministic_book_keys_includes_pages() -> None:
    keys = deterministic_book_keys("b1", page_count=2)
    assert "pdfs/b1.pdf" in keys
    assert "covers/b1" in keys
    assert "pages/b1/0001.png" in keys
    assert "pages/b1/0002.png" in keys


def test_collect_book_keys_unions_referenced_keys() -> None:
    keys = collect_book_keys(
        "b1",
        page_count=1,
        book_row={"source_pdf_key": "pdfs/b1.pdf", "cover_key": "covers/b1"},
        pages=[{"image_key": "pages/b1/0001.png"}],
        entities=[{"appearance": {"reference_image_keys": ["refs/b1/char_x/f.png"]}}],
        shots=[{"output": {"clip_key": "clips/b1/s1.mp4", "last_frame_key": "lastframes/b1/s1.png"},
                "narration": {"audio_key": "audio/b1/s1.wav"}}],
        shot_cache=[{"clip_key": "clips/b1/cached.mp4"}],
    )
    assert "clips/b1/s1.mp4" in keys
    assert "audio/b1/s1.wav" in keys
    assert "refs/b1/char_x/f.png" in keys
    assert "clips/b1/cached.mp4" in keys
    assert "lastframes/b1/s1.png" in keys
