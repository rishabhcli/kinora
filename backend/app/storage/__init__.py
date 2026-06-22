"""S3-compatible object storage (MinIO locally, OSS/S3 in prod).

One :class:`~app.storage.object_store.ObjectStore` client wraps boto3 with
path-style addressing (required by MinIO) and Signature V4. Object keys are
built through the helpers in :mod:`app.storage.object_store` so the layout
(``clips/``, ``keyframes/``, ``audio/``, ``refs/``, ``lastframes/``, ``pdfs/``,
``canon/``) stays consistent across the codebase.
"""

from __future__ import annotations

from app.storage.object_store import ObjectStore, keys

__all__ = ["ObjectStore", "keys"]
