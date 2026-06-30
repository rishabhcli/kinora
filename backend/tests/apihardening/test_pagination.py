"""Cursor pagination: signed opaque cursors + Page[T] (pure, deterministic)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.apihardening.pagination import (
    CURSOR_VERSION,
    Cursor,
    CursorCodec,
    Page,
    PageMeta,
    decode_cursor,
    encode_cursor,
    paginate,
)
from app.apihardening.problem import ProblemException


def test_cursor_roundtrip() -> None:
    codec = CursorCodec("secret")
    token = encode_cursor(codec, key=42, id="row-7")
    cursor = codec.decode(token)
    assert cursor.key == 42
    assert cursor.id == "row-7"
    assert cursor.direction == "next"
    assert cursor.v == CURSOR_VERSION


def test_cursor_is_opaque_urlsafe() -> None:
    codec = CursorCodec("secret")
    token = encode_cursor(codec, key="2026-06-29T00:00:00Z")
    # URL-safe base64 + a single dot separator; no raw payload leakage.
    assert "." in token
    assert "+" not in token and "/" not in token and " " not in token


def test_tampered_cursor_is_rejected() -> None:
    codec = CursorCodec("secret")
    token = encode_cursor(codec, key=1)
    payload, sig = token.split(".", 1)
    tampered = payload[:-1] + ("A" if payload[-1] != "A" else "B") + "." + sig
    with pytest.raises(ProblemException) as ei:
        codec.decode(tampered)
    assert ei.value.code == "invalid_cursor"
    assert ei.value.status == 422


def test_cursor_signed_with_other_secret_is_rejected() -> None:
    token = encode_cursor(CursorCodec("secret-a"), key=1)
    with pytest.raises(ProblemException):
        CursorCodec("secret-b").decode(token)


def test_cursor_namespace_isolation() -> None:
    token = encode_cursor(CursorCodec("s", namespace="books"), key=1)
    # A cursor minted for the books endpoint can't be replayed against shots.
    with pytest.raises(ProblemException):
        CursorCodec("s", namespace="shots").decode(token)


def test_malformed_cursor_strings_rejected() -> None:
    codec = CursorCodec("secret")
    for bad in ["", "no-dot", "a.b", "!!!.@@@", "onlypayload."]:
        with pytest.raises(ProblemException):
            codec.decode(bad)


def test_decode_cursor_none_is_first_page() -> None:
    codec = CursorCodec("secret")
    assert decode_cursor(codec, None) is None
    assert decode_cursor(codec, "") is None


def test_paginate_trims_extra_and_sets_has_more() -> None:
    codec = CursorCodec("secret")
    rows = [{"id": i, "sort": i * 10} for i in range(1, 7)]  # 6 rows, limit 5
    page = paginate(
        rows,
        limit=5,
        codec=codec,
        key_of=lambda r: r["sort"],
        id_of=lambda r: str(r["id"]),
    )
    assert page.page.count == 5
    assert page.page.has_more is True
    assert page.page.next_cursor is not None
    # The next cursor points at the last *kept* row's sort key.
    cursor = codec.decode(page.page.next_cursor)
    assert cursor.key == 50
    assert cursor.id == "5"


def test_paginate_last_page_has_no_cursor() -> None:
    codec = CursorCodec("secret")
    rows = [{"id": i, "sort": i} for i in range(3)]  # 3 rows, limit 5 -> last page
    page = paginate(rows, limit=5, codec=codec, key_of=lambda r: r["sort"])
    assert page.page.has_more is False
    assert page.page.next_cursor is None
    assert page.page.count == 3


def test_generic_page_model_serializes() -> None:
    class Item(BaseModel):
        name: str

    page = Page[Item](
        items=[Item(name="a"), Item(name="b")],
        page=PageMeta(limit=10, count=2, has_more=False),
    )
    dumped = page.model_dump()
    assert dumped["items"] == [{"name": "a"}, {"name": "b"}]
    assert dumped["page"]["count"] == 2


def test_cursor_version_mismatch_rejected() -> None:
    codec = CursorCodec("secret")
    # Hand-mint a cursor with a future version and verify decode rejects it.
    token = codec.encode(Cursor(v=999, key=1))
    with pytest.raises(ProblemException):
        codec.decode(token)
