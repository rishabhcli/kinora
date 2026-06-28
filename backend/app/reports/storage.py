"""Report artifact storage + signed retrieval.

A rendered report's bytes live in object storage; the :class:`ReportArtifact`
row is the index over them. :class:`ReportArtifactStore` is the seam between the
two: it builds the canonical storage key, puts the bytes, and mints a
short-lived signed URL for retrieval — never a public path. The content hash is
a ``sha256`` of the rendered bytes so a re-render of identical content reuses the
same key (the §8.7 "a re-read costs nothing" idea, applied to documents).

The object-store dependency is the same :class:`~app.storage.object_store.ObjectStore`
the rest of the backend uses; this module only adds the report key layout + the
content-addressing helpers, so it composes with MinIO locally and OSS/S3 in
production with no new infra.
"""

from __future__ import annotations

import hashlib

from app.reports.render import ReportFormat, media_type
from app.storage.object_store import ObjectStore


def content_hash(data: bytes) -> str:
    """The sha256 hex digest of rendered artifact bytes (dedup + integrity)."""
    return hashlib.sha256(data).hexdigest()


def report_key(
    *,
    owner_id: str | None,
    kind: str,
    digest: str,
    fmt: ReportFormat,
) -> str:
    """Canonical object key for a rendered report.

    ``reports/{owner}/{kind}/{sha8}.{ext}`` — owner-scoped, content-addressed by
    the first 16 hex chars of the digest so identical content collapses to one
    object. Anonymous/operator reports use ``_`` as the owner segment.
    """
    owner = owner_id or "_"
    ext = media_type(fmt).extension
    return f"reports/{owner}/{kind}/{digest[:16]}.{ext}"


class ReportArtifactStore:
    """Persist rendered report bytes + mint signed retrieval URLs."""

    def __init__(self, object_store: ObjectStore) -> None:
        self._store = object_store

    def put(
        self,
        data: bytes,
        *,
        owner_id: str | None,
        kind: str,
        fmt: ReportFormat,
    ) -> tuple[str, str]:
        """Store ``data`` and return ``(storage_key, content_hash)``.

        Idempotent: the key is content-addressed, so re-storing identical bytes
        overwrites with the same bytes at the same key (a no-op for callers that
        already deduped on the hash).
        """
        digest = content_hash(data)
        key = report_key(owner_id=owner_id, kind=kind, digest=digest, fmt=fmt)
        self._store.put_bytes(key, data, content_type=media_type(fmt).content_type)
        return key, digest

    def signed_url(self, storage_key: str, *, ttl: int = 3600) -> str:
        """A short-lived signed/presigned URL for a stored artifact."""
        return self._store.presigned_get_url(storage_key, ttl=ttl)

    def fetch(self, storage_key: str) -> bytes:
        """Download a stored artifact's bytes (server-side retrieval)."""
        return self._store.get_bytes(storage_key)

    def exists(self, storage_key: str) -> bool:
        """Whether the artifact object is present."""
        return self._store.exists(storage_key)

    def delete(self, storage_key: str) -> None:
        """Delete a stored artifact (retention sweep)."""
        self._store.delete(storage_key)


__all__ = ["ReportArtifactStore", "content_hash", "report_key"]
