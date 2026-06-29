"""A tiny content-addressable blob store seam for warehouse files.

The catalog records *paths*; the bytes live in a :class:`BlobStore`. In production
that is OSS/MinIO; for tests and the deterministic core it is
:class:`InMemoryBlobStore`. Keys are content hashes so writing the same bytes twice
is idempotent and a snapshot referencing a file is stable across runs.
"""

from __future__ import annotations

import hashlib
from typing import Protocol, runtime_checkable


@runtime_checkable
class BlobStore(Protocol):
    """Persist and fetch immutable byte blobs by key."""

    def put(self, data: bytes) -> str:
        """Store ``data``; return its content-addressed key."""
        ...

    def get(self, key: str) -> bytes:
        """Fetch the blob at ``key`` (raises :class:`KeyError` if absent)."""
        ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...


def content_key(data: bytes, *, prefix: str = "klwf") -> str:
    digest = hashlib.blake2b(data, digest_size=16).hexdigest()
    return f"{prefix}/{digest}"


class InMemoryBlobStore:
    """A deterministic, content-addressed, in-memory blob store."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, data: bytes) -> str:
        key = content_key(data)
        self._blobs.setdefault(key, data)
        return key

    def get(self, key: str) -> bytes:
        try:
            return self._blobs[key]
        except KeyError as exc:
            raise KeyError(f"no blob at {key}") from exc

    def exists(self, key: str) -> bool:
        return key in self._blobs

    def delete(self, key: str) -> None:
        self._blobs.pop(key, None)

    def __len__(self) -> int:
        return len(self._blobs)

    @property
    def total_bytes(self) -> int:
        return sum(len(b) for b in self._blobs.values())


__all__ = ["BlobStore", "InMemoryBlobStore", "content_key"]
