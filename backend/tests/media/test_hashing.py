"""Unit tests for content hashing & content-address key derivation (§8.7)."""

from __future__ import annotations

import hashlib
import io

import pytest

from app.media.hashing import (
    CONTENT_ADDRESS_PREFIX,
    content_address_key,
    digest_from_key,
    sha256_chunks,
    sha256_hex,
    sha256_stream,
    short_digest,
)


def test_sha256_hex_matches_hashlib() -> None:
    data = b"watch the book"
    assert sha256_hex(data) == hashlib.sha256(data).hexdigest()


def test_sha256_stream_equals_whole() -> None:
    data = b"x" * (3 * 1024 * 1024 + 17)
    assert sha256_stream(io.BytesIO(data), chunk=1024) == sha256_hex(data)


def test_sha256_chunks_order_sensitive() -> None:
    assert sha256_chunks([b"ab", b"cd"]) == sha256_hex(b"abcd")
    assert sha256_chunks([b"cd", b"ab"]) != sha256_hex(b"abcd")


def test_content_address_key_fans_out_and_keeps_suffix() -> None:
    digest = sha256_hex(b"poster-bytes")
    key = content_address_key(digest, suffix=".png")
    assert key == f"{CONTENT_ADDRESS_PREFIX}/{digest[:2]}/{digest[2:4]}/{digest}.png"
    # suffix dot is optional
    assert content_address_key(digest, suffix="png") == key


def test_content_address_key_custom_prefix() -> None:
    digest = sha256_hex(b"z")
    key = content_address_key(digest, suffix=".mp4", prefix="books/abc/blobs")
    assert key.startswith("books/abc/blobs/")
    assert key.endswith(f"{digest}.mp4")


def test_content_address_key_is_deterministic_for_identical_bytes() -> None:
    a = content_address_key(sha256_hex(b"same"), suffix=".mp4")
    b = content_address_key(sha256_hex(b"same"), suffix=".mp4")
    assert a == b


@pytest.mark.parametrize("bad", ["", "abc", "X" * 64, "g" * 64])
def test_content_address_key_rejects_non_digest(bad: str) -> None:
    with pytest.raises(ValueError):
        content_address_key(bad)


def test_digest_from_key_roundtrips() -> None:
    digest = sha256_hex(b"roundtrip")
    key = content_address_key(digest, suffix=".webp")
    assert digest_from_key(key) == digest
    assert digest_from_key("clips/book/shot_1.mp4") is None


def test_short_digest() -> None:
    digest = sha256_hex(b"q")
    assert short_digest(digest) == digest[:12]
    assert short_digest(digest, length=4) == digest[:4]
