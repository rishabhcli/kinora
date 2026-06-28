"""Media / asset service (``app.media``).

A hardened media-asset subsystem layered **over** :class:`app.storage.object_store.ObjectStore`
and **complementing** the §9.7 render pipeline (which already persists provider
videos because task URLs expire). See ``DESIGN.md`` in this package for the full
architecture and milestone roadmap.

Public surface (import-light; heavy submodules — ffmpeg/packaging — are imported
locally where used so ``import app.media`` stays cheap):

* :mod:`app.media.errors` — typed errors.
* :mod:`app.media.hashing` — streaming sha256 + content-address keys (§8.7).
* :mod:`app.media.urls` — URL signing contract + ``minio:9000``→``localhost:9000``.
* :mod:`app.media.metadata` — :class:`AssetMetadata` + MIME/extension helpers.
* :mod:`app.media.kinds` — :class:`MediaAssetKind`.
* :mod:`app.media.store` — :class:`MediaStore` (content-addressed, multipart).
* :mod:`app.media.service` — :class:`MediaService` orchestration facade.
* :mod:`app.media.ranges` — HTTP byte-range parsing for progressive playback.
"""

from __future__ import annotations

from app.media.errors import (
    ChecksumMismatchError,
    MediaError,
    MultipartError,
    PackagingError,
    UploadNotFoundError,
)
from app.media.hashing import (
    content_address_key,
    sha256_bytes,
    sha256_hex,
    short_digest,
)
from app.media.kinds import MediaAssetKind
from app.media.metadata import AssetMetadata, guess_content_type, suffix_for
from app.media.ranges import ByteRange, RangeNotSatisfiableError, parse_range

__all__ = [
    "AssetMetadata",
    "ByteRange",
    "ChecksumMismatchError",
    "MediaAssetKind",
    "MediaError",
    "MultipartError",
    "PackagingError",
    "RangeNotSatisfiableError",
    "UploadNotFoundError",
    "content_address_key",
    "guess_content_type",
    "parse_range",
    "sha256_bytes",
    "sha256_hex",
    "short_digest",
    "suffix_for",
]
