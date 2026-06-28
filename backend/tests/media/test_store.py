"""Unit tests for the content-addressed MediaStore + multipart upload."""

from __future__ import annotations

import pytest

from app.media.errors import ChecksumMismatchError, MultipartError, UploadNotFoundError
from app.media.hashing import content_address_key, sha256_hex
from app.media.kinds import MediaAssetKind
from app.media.store import MIN_PART_BYTES, MediaStore, chunked
from app.media.testing import FakeMediaStore


@pytest.fixture
def store() -> MediaStore:
    return MediaStore(FakeMediaStore())


def test_put_and_get_roundtrip(store: MediaStore) -> None:
    meta = store.put("clips/b/s.mp4", b"hello", "video/mp4")
    assert meta.content_type == "video/mp4"
    assert meta.content_hash == sha256_hex(b"hello")
    assert meta.size_bytes == 5
    assert store.get("clips/b/s.mp4") == b"hello"
    assert store.exists("clips/b/s.mp4")


def test_put_infers_content_type(store: MediaStore) -> None:
    meta = store.put("a/poster.png", b"\x89PNG\r\n\x1a\n")
    assert meta.content_type == "image/png"


def test_content_addressed_dedups_identical_bytes() -> None:
    backend = FakeMediaStore()
    store = MediaStore(backend)
    data = b"identical-card-bytes"

    meta1, dedup1 = store.put_content_addressed(data, suffix=".mp4", kind=MediaAssetKind.CLIP)
    assert dedup1 is False
    assert backend.put_calls == 1

    meta2, dedup2 = store.put_content_addressed(data, suffix=".mp4", kind=MediaAssetKind.CLIP)
    assert dedup2 is True
    assert backend.put_calls == 1  # no second upload
    assert meta1.storage_key == meta2.storage_key
    assert meta1.content_hash == meta2.content_hash


def test_content_addressed_key_matches_helper(store: MediaStore) -> None:
    data = b"poster"
    meta, _ = store.put_content_addressed(data, suffix=".png")
    assert meta.storage_key == content_address_key(sha256_hex(data), suffix=".png")


def test_address_of_is_pure(store: MediaStore) -> None:
    key = store.address_of(b"x", suffix=".mp4")
    assert not store.exists(key)  # address_of does not upload


def test_get_verified_ok(store: MediaStore) -> None:
    data = b"verify-me"
    store.put("k", data)
    assert store.get_verified("k", sha256_hex(data)) == data


def test_get_verified_mismatch(store: MediaStore) -> None:
    store.put("k", b"actual")
    with pytest.raises(ChecksumMismatchError):
        store.get_verified("k", sha256_hex(b"expected"))


def test_url_for_uses_signer(store: MediaStore) -> None:
    store.put("k", b"x")
    assert store.url_for("k").startswith("https://signed.invalid/k")


# -- multipart --------------------------------------------------------------- #


def test_multipart_rejects_small_nonfinal_part(store: MediaStore) -> None:
    up = store.create_multipart(content_type="video/mp4")
    with pytest.raises(MultipartError):
        up.upload_part(b"tiny", is_final=False)


def test_multipart_complete_content_addressed() -> None:
    backend = FakeMediaStore()
    store = MediaStore(backend)
    big = b"A" * MIN_PART_BYTES
    tail = b"BBB"
    up = store.create_multipart(content_type="video/mp4")
    assert up.upload_part(big, is_final=False) == 1
    assert up.upload_part(tail, is_final=True) == 2
    assert up.part_count == 2
    assert up.size_bytes == len(big) + len(tail)

    meta, dedup = store.complete_multipart(up, suffix=".mp4", kind=MediaAssetKind.SOURCE)
    assert dedup is False
    assert meta.content_hash == sha256_hex(big + tail)
    assert store.get(meta.storage_key) == big + tail


def test_multipart_resume_via_get_multipart() -> None:
    store = MediaStore(FakeMediaStore())
    up = store.create_multipart()
    up.upload_part(b"A" * MIN_PART_BYTES, is_final=False)
    # resume by id
    resumed = store.get_multipart(up.upload_id)
    assert resumed is up
    resumed.upload_part(b"end", is_final=True)
    meta, _ = store.complete_multipart(resumed)
    assert meta.size_bytes == MIN_PART_BYTES + 3


def test_multipart_abort_invalidates() -> None:
    store = MediaStore(FakeMediaStore())
    up = store.create_multipart()
    store.abort_multipart(up)
    assert up.is_open is False
    with pytest.raises(UploadNotFoundError):
        store.get_multipart(up.upload_id)


def test_complete_after_abort_raises() -> None:
    store = MediaStore(FakeMediaStore())
    up = store.create_multipart()
    store.abort_multipart(up)
    with pytest.raises(UploadNotFoundError):
        store.complete_multipart(up)


def test_complete_with_expected_hash_mismatch() -> None:
    store = MediaStore(FakeMediaStore())
    up = store.create_multipart()
    up.upload_part(b"only", is_final=True)
    with pytest.raises(ChecksumMismatchError):
        store.complete_multipart(up, expected_hash=sha256_hex(b"different"))


def test_put_stream_coalesces_parts() -> None:
    backend = FakeMediaStore()
    store = MediaStore(backend)
    payload = b"Z" * (MIN_PART_BYTES * 2 + 100)
    meta, dedup = store.put_stream(
        chunked(payload, 7919),  # awkward chunk size, prime
        suffix=".pdf",
        kind=MediaAssetKind.SOURCE,
        part_bytes=MIN_PART_BYTES,
    )
    assert dedup is False
    assert meta.content_hash == sha256_hex(payload)
    assert store.get(meta.storage_key) == payload


def test_put_stream_then_dedups() -> None:
    backend = FakeMediaStore()
    store = MediaStore(backend)
    payload = b"Q" * (MIN_PART_BYTES + 1)
    store.put_stream(chunked(payload, 1000), suffix=".mp4")
    calls = backend.put_calls
    _, dedup = store.put_stream(chunked(payload, 1000), suffix=".mp4")
    assert dedup is True
    assert backend.put_calls == calls
