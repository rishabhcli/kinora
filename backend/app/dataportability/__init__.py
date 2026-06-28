"""Data export / import & portability (the §8/§9 durable state, made portable).

A book's durable state lives in two places: **Postgres** (the canon graph, shots,
scenes, beats, sync maps, budget ledger, prefs, defects) and **object storage**
(source PDF/EPUB, page images, keyframes, reference assets, rendered clips,
last-frames, narration audio, the markdown canon vault). This package makes that
state portable as a single versioned, checksummed ``.kinora`` archive:

* :mod:`app.dataportability.codec` — the streaming, checksummed ZIP container;
* :mod:`app.dataportability.book_export` / ``book_import`` — full book bundle;
* :mod:`app.dataportability.canon_export` / ``canon_import`` — canon-only;
* :mod:`app.dataportability.account` — GDPR export + right-to-erasure;
* :mod:`app.dataportability.backup` — backup + point-in-time restore;
* :mod:`app.dataportability.migrate` — archive-format version upgrades on import;
* :mod:`app.dataportability.service` — the façade the HTTP route calls.

See ``DESIGN.md`` in this package for the archive format and the roadmap.
"""

from __future__ import annotations

from app.dataportability.errors import (
    ArchiveFormatError,
    ArchiveKindMismatchError,
    ChecksumMismatchError,
    PortabilityError,
    ReferentialIntegrityError,
    UnsupportedArchiveVersionError,
)
from app.dataportability.manifest import (
    CURRENT_FORMAT_VERSION,
    MIN_SUPPORTED_FORMAT_VERSION,
    ArchiveKind,
    ArchiveManifest,
    BlobRef,
    sha256_hex,
)
from app.dataportability.service import ArchiveInspection, PortabilityService

__all__ = [
    "CURRENT_FORMAT_VERSION",
    "MIN_SUPPORTED_FORMAT_VERSION",
    "ArchiveFormatError",
    "ArchiveInspection",
    "ArchiveKind",
    "ArchiveKindMismatchError",
    "ArchiveManifest",
    "BlobRef",
    "ChecksumMismatchError",
    "PortabilityError",
    "PortabilityService",
    "ReferentialIntegrityError",
    "UnsupportedArchiveVersionError",
    "sha256_hex",
]
