"""Unit tests for the archive codec + manifest (no infrastructure).

These exercise the streaming, checksummed ZIP container directly: round-trip of
rows + blobs, content-addressing, checksum/tamper detection, the manifest digest,
and bounded-memory streaming. They do not touch the DB, Redis, or object storage.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.dataportability.codec import ArchiveReader, open_writer_to_bytes
from app.dataportability.errors import ArchiveFormatError, ChecksumMismatchError
from app.dataportability.manifest import (
    CURRENT_FORMAT_VERSION,
    ArchiveKind,
    ArchiveManifest,
    BlobRef,
    sha256_hex,
)


def _blob_ref(payload: bytes, key: str, ct: str | None = None) -> tuple[str, BlobRef]:
    sha = sha256_hex(payload)
    return sha, BlobRef(sha256=sha, size=len(payload), content_type=ct, original_key=key)


def test_manifest_digest_is_order_independent() -> None:
    a = ArchiveManifest(checksums={"data/x.jsonl": "aa", "data/y.jsonl": "bb"})
    b = ArchiveManifest(checksums={"data/y.jsonl": "bb", "data/x.jsonl": "aa"})
    assert a.compute_digest() == b.compute_digest()


def test_manifest_digest_changes_with_content() -> None:
    a = ArchiveManifest(checksums={"data/x.jsonl": "aa"})
    b = ArchiveManifest(checksums={"data/x.jsonl": "ab"})
    assert a.compute_digest() != b.compute_digest()


def test_manifest_json_round_trip() -> None:
    m = ArchiveManifest(
        kind=ArchiveKind.BOOK, meta={"book_id": "b1"}, counts={"shots": 3}
    ).sealed()
    parsed = ArchiveManifest.from_json_bytes(m.to_json_bytes())
    assert parsed.kind == ArchiveKind.BOOK
    assert parsed.meta == {"book_id": "b1"}
    assert parsed.counts == {"shots": 3}
    assert parsed.manifest_digest == m.compute_digest()


def test_round_trip_rows_and_blobs() -> None:
    writer, buffer = open_writer_to_bytes(ArchiveManifest(kind=ArchiveKind.BOOK))
    payload = b"hello world" * 1000
    sha, ref = _blob_ref(payload, "clips/b1/shot1.mp4", "video/mp4")
    with writer as w:
        n = w.write_rows("shots", [{"id": "s1", "prompt": "x"}, {"id": "s2", "prompt": "y"}])
        assert n == 2
        w.write_blob(sha, [payload[:100], payload[100:]], ref)

    buffer.seek(0)
    with ArchiveReader(buffer) as r:
        r.verify()  # no exception
        assert r.manifest.format_version == CURRENT_FORMAT_VERSION
        assert r.manifest.counts == {"shots": 2}
        rows = list(r.read_rows("shots"))
        assert rows == [{"id": "s1", "prompt": "x"}, {"id": "s2", "prompt": "y"}]
        refs = r.blob_refs()
        assert sha in refs
        assert refs[sha].original_key == "clips/b1/shot1.mp4"
        assert refs[sha].content_type == "video/mp4"
        assert r.read_blob(sha) == payload


def test_missing_table_yields_nothing() -> None:
    writer, buffer = open_writer_to_bytes(ArchiveManifest())
    with writer as w:
        w.write_rows("shots", [{"id": "s1"}])
    buffer.seek(0)
    with ArchiveReader(buffer) as r:
        assert list(r.read_rows("nonexistent")) == []


def test_empty_archive_round_trips() -> None:
    writer, buffer = open_writer_to_bytes(ArchiveManifest())
    with writer:
        pass
    buffer.seek(0)
    with ArchiveReader(buffer) as r:
        r.verify()
        assert r.tables() == []
        assert r.blob_refs() == {}


def test_blob_dedup_writes_once() -> None:
    writer, buffer = open_writer_to_bytes(ArchiveManifest())
    payload = b"shared keyframe bytes"
    sha, ref = _blob_ref(payload, "keyframes/b1/beat1.png")
    sha2, ref2 = _blob_ref(payload, "keyframes/b1/beat2.png")
    assert sha == sha2  # identical content -> identical sha
    with writer as w:
        w.write_blob(sha, [payload], ref)
        w.write_blob(sha2, [payload], ref2)  # dedup no-op
    buffer.seek(0)
    with ArchiveReader(buffer) as r:
        refs = r.blob_refs()
        assert len(refs) == 1  # stored once


def test_declared_sha_must_match_content() -> None:
    writer, buffer = open_writer_to_bytes(ArchiveManifest())
    with writer as w, pytest.raises(ArchiveFormatError):
        w.write_blob("deadbeef" * 8, [b"actual content"], BlobRef(
            sha256="deadbeef" * 8, size=14, original_key="k"
        ))


def test_tampered_data_member_detected() -> None:
    writer, buffer = open_writer_to_bytes(ArchiveManifest())
    with writer as w:
        w.write_rows("shots", [{"id": "s1"}])
    # Rebuild the zip with a tampered data member but the ORIGINAL manifest.
    tampered = _rewrite_member(buffer.getvalue(), "data/shots.jsonl", b'{"id":"HACKED"}\n')
    with ArchiveReader(io.BytesIO(tampered)) as r, pytest.raises(ChecksumMismatchError) as ei:
        r.verify()
    assert ei.value.member == "data/shots.jsonl"


def test_tampered_blob_detected_on_stream() -> None:
    writer, buffer = open_writer_to_bytes(ArchiveManifest())
    payload = b"original clip bytes"
    sha, ref = _blob_ref(payload, "clips/b1/s1.mp4")
    with writer as w:
        w.write_blob(sha, [payload], ref)
    member = f"blobs/{sha}"
    tampered = _rewrite_member(buffer.getvalue(), member, b"tampered clip bytes!")
    with ArchiveReader(io.BytesIO(tampered)) as r, pytest.raises(ChecksumMismatchError):
        # Even a streamed read of the single blob catches the tamper.
        list(r.open_blob(sha))


def test_streamed_row_read_detects_tamper() -> None:
    writer, buffer = open_writer_to_bytes(ArchiveManifest())
    with writer as w:
        w.write_rows("shots", [{"id": f"s{i}"} for i in range(50)])
    tampered = _rewrite_member(
        buffer.getvalue(), "data/shots.jsonl", b'{"id":"x"}\n' * 50
    )
    with ArchiveReader(io.BytesIO(tampered)) as r, pytest.raises(ChecksumMismatchError):
        list(r.read_rows("shots", verify=True))


def test_bad_zip_raises_format_error() -> None:
    with pytest.raises(ArchiveFormatError):
        ArchiveReader(io.BytesIO(b"this is not a zip file"))


def test_missing_manifest_raises_format_error() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("data/shots.jsonl", b'{"id":"s1"}\n')
    buf.seek(0)
    with pytest.raises(ArchiveFormatError):
        ArchiveReader(buf)


def test_large_blob_streams_in_chunks() -> None:
    # A 4 MiB blob exercises the multi-chunk read/write path (BLOB_CHUNK = 1 MiB).
    payload = bytes((i % 251) for i in range(4 * (1 << 20)))
    sha, ref = _blob_ref(payload, "clips/b1/big.mp4", "video/mp4")
    writer, buffer = open_writer_to_bytes(ArchiveManifest())
    with writer as w:
        # Feed in many small chunks to prove streaming.
        w.write_blob(sha, (payload[i : i + 7777] for i in range(0, len(payload), 7777)), ref)
    buffer.seek(0)
    with ArchiveReader(buffer) as r:
        out = b"".join(r.open_blob(sha))
        assert out == payload
        assert r.blob_refs()[sha].size == len(payload)


def _rewrite_member(zip_bytes: bytes, member: str, new_content: bytes) -> bytes:
    """Return a copy of ``zip_bytes`` with one member's content replaced."""
    src = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as dst:
        for name in src.namelist():
            data = new_content if name == member else src.read(name)
            dst.writestr(name, data)
    src.close()
    return out.getvalue()
