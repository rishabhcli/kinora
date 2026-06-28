"""The archive manifest — the versioned, checksummed table of contents.

A ``.kinora`` archive opens with ``manifest.json``. The manifest declares the
**format version** (so the migration layer can upgrade an old archive on
import), the archive **kind** (book bundle / canon-only / account / backup), the
**checksums** of every data member and blob (so ``verify()`` can detect a
truncated or tampered archive), and small **counts/metadata** for inspection
without unpacking the whole thing.

The manifest also carries a ``manifest_digest`` — a SHA-256 over the *sorted*
checksum index — so a single value attests the integrity of the whole archive.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

#: The current on-disk archive format version. Bump this and register a
#: ``vN -> vN+1`` transform in :mod:`app.dataportability.migrate` whenever the
#: serialized row shapes or layout change incompatibly.
CURRENT_FORMAT_VERSION = 1

#: The oldest ``format_version`` the migration chain still understands. Archives
#: older than this are rejected with :class:`UnsupportedArchiveVersionError`.
MIN_SUPPORTED_FORMAT_VERSION = 1


class ArchiveKind:
    """The kinds of archive this layer produces (string constants, not an enum
    on the wire so an older reader degrades gracefully on an unknown kind)."""

    BOOK = "book_bundle"
    CANON = "canon_graph"
    ACCOUNT = "account"
    BACKUP = "backup"


class BlobRef(BaseModel):
    """One content-addressed blob's metadata in ``blobs/index.jsonl``.

    The blob payload lives at ``blobs/<sha256>``; rows reference it by ``sha256``.
    ``original_key`` is the object-store key the blob came from, used to restore
    it to the same logical location (remapped to the new ``book_id`` on import).
    """

    sha256: str
    size: int
    content_type: str | None = None
    original_key: str


class ArchiveManifest(BaseModel):
    """The archive's table of contents and integrity index."""

    format_version: int = CURRENT_FORMAT_VERSION
    kind: str = ArchiveKind.BOOK
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    #: Free-form, kind-specific metadata (e.g. exported book ids, titles, owner).
    meta: dict[str, Any] = Field(default_factory=dict)
    #: Row counts per logical table (``data/<table>.jsonl``) — for inspection.
    counts: dict[str, int] = Field(default_factory=dict)
    #: member-name -> sha256 of its raw bytes. Covers ``data/*.jsonl`` AND
    #: ``blobs/index.jsonl``; individual blob payloads are checksummed by name
    #: (the name *is* their sha256) and re-listed here for a single integrity set.
    checksums: dict[str, str] = Field(default_factory=dict)
    #: SHA-256 over the sorted ``checksums`` index — attests the whole archive.
    manifest_digest: str = ""

    def compute_digest(self) -> str:
        """Recompute the manifest digest from the current checksum index.

        The digest is order-independent: it hashes the checksum entries sorted by
        member name, so two archives with the same content always agree.
        """
        lines = [f"{name}\x00{digest}" for name, digest in sorted(self.checksums.items())]
        payload = "\n".join(lines).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def sealed(self) -> ArchiveManifest:
        """Return a copy with ``manifest_digest`` set from the checksum index."""
        return self.model_copy(update={"manifest_digest": self.compute_digest()})

    def to_json_bytes(self) -> bytes:
        """Serialize to canonical (sorted-key) UTF-8 JSON bytes."""
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, indent=2
        ).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, data: bytes) -> ArchiveManifest:
        """Parse a manifest from its JSON bytes."""
        return cls.model_validate_json(data)


def sha256_hex(data: bytes) -> str:
    """SHA-256 hex digest of ``data`` (the archive's checksum primitive)."""
    return hashlib.sha256(data).hexdigest()


__all__ = [
    "CURRENT_FORMAT_VERSION",
    "MIN_SUPPORTED_FORMAT_VERSION",
    "ArchiveKind",
    "ArchiveManifest",
    "BlobRef",
    "sha256_hex",
]
