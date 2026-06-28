"""Bridge object storage ↔ archive blobs (content-addressed, streamed, dedup).

Export reads a set of object-store keys and writes each payload into the archive
as a content-addressed blob, recording a :class:`BlobRef` (sha + original key +
content type). Two keys with identical content share one blob (dedup by sha).

Import restores blobs to object storage at their *rewritten* keys (the book-id
segment remapped to the new book).

All object-store calls are blocking boto3; they run off the event loop via
``anyio.to_thread``. The payload itself is read whole per object (S3 ``get`` is a
single call) — this is the one place memory scales with the largest single asset
(a clip), which is acceptable and matches how the render pipeline already loads
clips. The *archive* side still streams the bytes into the zip in chunks.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

import anyio

from app.dataportability.codec import BLOB_CHUNK, ArchiveReader, ArchiveWriter
from app.dataportability.keys import rewrite_key_book_id
from app.dataportability.manifest import BlobRef, sha256_hex
from app.memory.interfaces import BlobStore


@runtime_checkable
class DeletableBlobStore(BlobStore, Protocol):
    """A :class:`BlobStore` that also supports deletion (the erasure path).

    The memory layer's :class:`BlobStore` is read/write only; right-to-erasure
    additionally needs ``delete``. The real :class:`app.storage.object_store.ObjectStore`
    satisfies this wider protocol, so the eraser depends on it explicitly rather
    than reaching past the narrower seam.
    """

    def delete(self, key: str) -> None: ...


def _guess_content_type(key: str) -> str | None:
    """Best-effort content type from a key's extension (cosmetic, for the index)."""
    lower = key.lower()
    if lower.endswith(".mp4"):
        return "video/mp4"
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".wav"):
        return "audio/wav"
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith(".epub"):
        return "application/epub+zip"
    if lower.endswith(".md"):
        return "text/markdown"
    return None


class BlobExporter:
    """Pull object-store keys into an :class:`ArchiveWriter` (dedup by content)."""

    def __init__(self, store: BlobStore) -> None:
        self._store = store
        self._seen: set[str] = set()
        self.exported = 0
        self.missing: list[str] = []

    async def export_keys(self, writer: ArchiveWriter, keys: Iterable[str]) -> None:
        """Write each existing key as a blob; silently skip absent keys.

        Absent keys are recorded in :attr:`missing` (not an error): a book may
        reference a cover or EPUB it never had, and an export of a partially
        rendered book legitimately has no clips yet.
        """
        for key in keys:
            await self._export_one(writer, key)

    async def _export_one(self, writer: ArchiveWriter, key: str) -> None:
        exists = await anyio.to_thread.run_sync(self._store.exists, key)
        if not exists:
            self.missing.append(key)
            return
        payload = await anyio.to_thread.run_sync(self._store.get_bytes, key)
        sha = sha256_hex(payload)
        ref = BlobRef(
            sha256=sha,
            size=len(payload),
            content_type=_guess_content_type(key),
            original_key=key,
        )
        # write_blob is a dedup no-op for a sha we already wrote; still record the
        # association so the manifest's blob index lists every distinct content.
        if sha not in self._seen:
            writer.write_blob(sha, _chunks(payload), ref)
            self._seen.add(sha)
        self.exported += 1


class BlobImporter:
    """Restore archive blobs to object storage at rewritten keys."""

    def __init__(self, store: BlobStore) -> None:
        self._store = store
        self.restored = 0

    async def restore_all(
        self, reader: ArchiveReader, *, old_book_id: str, new_book_id: str
    ) -> None:
        """Restore every blob to ``original_key`` rewritten to the new book id.

        A blob whose original key does not embed the old book id (it never should
        for a book bundle) is restored verbatim. Content is verified against its
        sha as it streams out of the archive (``open_blob`` checks the hash).
        """
        for sha, ref in reader.blob_refs().items():
            target = rewrite_key_book_id(ref.original_key, old_book_id, new_book_id)
            payload = b"".join(reader.open_blob(sha, verify=True))
            await anyio.to_thread.run_sync(
                self._store.put_bytes, target, payload, ref.content_type
            )
            self.restored += 1


def _chunks(payload: bytes, size: int = BLOB_CHUNK) -> Iterable[bytes]:
    """Yield ``payload`` in fixed-size chunks (streams the archive write)."""
    for i in range(0, len(payload), size):
        yield payload[i : i + size]


__all__ = ["BlobExporter", "BlobImporter"]
