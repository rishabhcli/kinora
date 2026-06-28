"""Unit tests for blob bridge + service inspection (no DB, in-memory store).

A tiny in-memory object store stands in for MinIO so the blob export/import
bridge and ``PortabilityService.inspect_archive`` are covered without infra.
"""

from __future__ import annotations

import io

import pytest

from app.dataportability.blobs import BlobExporter, BlobImporter
from app.dataportability.codec import ArchiveReader, open_writer_to_bytes
from app.dataportability.manifest import ArchiveKind, ArchiveManifest
from app.dataportability.service import PortabilityService


class InMemoryStore:
    """A minimal in-memory BlobStore/DeletableBlobStore for unit tests."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[key] = data

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def presigned_get_url(self, key: str, ttl: int = 3600) -> str:
        return f"mem://{key}"


@pytest.mark.asyncio
async def test_blob_exporter_dedup_and_missing() -> None:
    store = InMemoryStore()
    store.put_bytes("clips/b1/a.mp4", b"same-bytes")
    store.put_bytes("clips/b1/b.mp4", b"same-bytes")  # identical content -> dedup
    store.put_bytes("refs/b1/x.png", b"unique")
    # "covers/b1" intentionally absent -> recorded as missing, not an error

    writer, buffer = open_writer_to_bytes(ArchiveManifest(kind=ArchiveKind.BOOK))
    exporter = BlobExporter(store)
    with writer as w:
        await exporter.export_keys(
            w, ["clips/b1/a.mp4", "clips/b1/b.mp4", "refs/b1/x.png", "covers/b1"]
        )
    assert exporter.exported == 3  # 3 existing keys touched
    assert exporter.missing == ["covers/b1"]

    buffer.seek(0)
    with ArchiveReader(buffer) as reader:
        refs = reader.blob_refs()
        assert len(refs) == 2  # only 2 distinct contents stored (dedup)


@pytest.mark.asyncio
async def test_blob_round_trip_rewrites_book_id() -> None:
    src = InMemoryStore()
    src.put_bytes("clips/OLD/a.mp4", b"clip-bytes")
    src.put_bytes("refs/OLD/face.png", b"ref-bytes")

    writer, buffer = open_writer_to_bytes(ArchiveManifest(kind=ArchiveKind.BOOK))
    with writer as w:
        await BlobExporter(src).export_keys(w, ["clips/OLD/a.mp4", "refs/OLD/face.png"])

    dst = InMemoryStore()
    buffer.seek(0)
    with ArchiveReader(buffer) as reader:
        importer = BlobImporter(dst)
        await importer.restore_all(reader, old_book_id="OLD", new_book_id="NEW")
    assert importer.restored == 2
    assert dst.objects["clips/NEW/a.mp4"] == b"clip-bytes"
    assert dst.objects["refs/NEW/face.png"] == b"ref-bytes"
    assert "clips/OLD/a.mp4" not in dst.objects  # re-homed, not duplicated


def test_inspect_archive_good() -> None:
    writer, buffer = open_writer_to_bytes(ArchiveManifest(kind=ArchiveKind.CANON))
    with writer as w:
        w.write_rows("entities", [{"id": "e1"}])
    inspection = PortabilityService.inspect_archive(buffer.getvalue())
    assert inspection.verified is True
    assert inspection.verify_error is None
    assert inspection.kind == ArchiveKind.CANON
    assert "entities" in inspection.tables


def test_inspect_archive_not_a_zip() -> None:
    inspection = PortabilityService.inspect_archive(b"definitely not a zip")
    assert inspection.verified is False
    assert inspection.verify_error is not None


def test_inspect_archive_tampered() -> None:
    import zipfile

    writer, buffer = open_writer_to_bytes(ArchiveManifest(kind=ArchiveKind.BOOK))
    with writer as w:
        w.write_rows("shots", [{"id": "s1"}])
    # Rebuild with a tampered data member but the original manifest.
    src = zipfile.ZipFile(io.BytesIO(buffer.getvalue()), "r")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for name in src.namelist():
            data = b'{"id":"HACKED"}\n' if name == "data/shots.jsonl" else src.read(name)
            dst.writestr(name, data)
    src.close()
    inspection = PortabilityService.inspect_archive(out.getvalue())
    assert inspection.verified is False
    assert "checksum" in (inspection.verify_error or "").lower()
