"""Archive-format migration — upgrade an old ``.kinora`` to the current schema.

Any durable format eventually outlives the schema it was written against. This
module is the forward-only migration chain: a registry of ``vN -> vN+1``
transforms applied in order so an archive written at any supported
``format_version`` reads as if it were written at
:data:`CURRENT_FORMAT_VERSION`.

A transform operates on a :class:`MigratedView` — a lazy, in-memory overlay on a
:class:`ArchiveReader`. It does **not** rewrite the zip on disk; it transforms
rows *as they are read*, so migration is cheap and import-time only. Each
transform is a pure function ``(manifest, rows_by_table) -> rows_by_table'`` plus
an optional manifest bump.

Adding a future format change is two steps:

1. bump :data:`CURRENT_FORMAT_VERSION` in ``manifest.py``;
2. register a ``@migration(from_version=N)`` transform here that turns a v``N``
   row set into a v``N+1`` row set.

The registry is validated at import time to be a contiguous chain with no gaps,
so a missing step is a loud failure, not a silent skip.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.dataportability.codec import ArchiveReader
from app.dataportability.errors import PortabilityError, UnsupportedArchiveVersionError
from app.dataportability.manifest import (
    CURRENT_FORMAT_VERSION,
    MIN_SUPPORTED_FORMAT_VERSION,
    ArchiveManifest,
)

#: A migration transforms a whole archive's row set from one version to the next.
#: ``(manifest, rows_by_table) -> rows_by_table'``. The manifest is read-only
#: input (for branching on ``kind``/``meta``); the version bump is implicit.
MigrationFn = Callable[
    [ArchiveManifest, dict[str, list[dict[str, Any]]]],
    dict[str, list[dict[str, Any]]],
]

#: from_version -> transform that produces ``from_version + 1``.
_REGISTRY: dict[int, MigrationFn] = {}


def migration(*, from_version: int) -> Callable[[MigrationFn], MigrationFn]:
    """Register ``fn`` as the ``from_version -> from_version+1`` transform."""

    def _register(fn: MigrationFn) -> MigrationFn:
        if from_version in _REGISTRY:
            raise PortabilityError(f"duplicate migration registered for v{from_version}")
        _REGISTRY[from_version] = fn
        return fn

    return _register


def _validate_chain() -> None:
    """Assert the registry forms a contiguous chain up to the current version."""
    expected = set(range(MIN_SUPPORTED_FORMAT_VERSION, CURRENT_FORMAT_VERSION))
    have = set(_REGISTRY)
    missing = expected - have
    if missing:
        raise PortabilityError(
            f"migration chain has gaps: missing transforms for versions {sorted(missing)}"
        )


class MigratedView:
    """A lazy, migrated overlay on an :class:`ArchiveReader`.

    If the archive is already at the current version, this is a thin pass-through
    that reads rows straight from the reader (no buffering). Otherwise it applies
    the migration chain once (buffering the affected tables — bounded per book)
    and serves the transformed rows.

    Blobs are never transformed by a migration in this design (object payloads are
    opaque and content-addressed), so :attr:`reader` exposes the underlying reader
    for blob restore.
    """

    def __init__(
        self,
        reader: ArchiveReader,
        manifest: ArchiveManifest,
        migrated_rows: dict[str, list[dict[str, Any]]] | None,
    ) -> None:
        self._reader = reader
        self._manifest = manifest
        self._migrated_rows = migrated_rows

    @property
    def manifest(self) -> ArchiveManifest:
        """The manifest as-of the current format version (kind/meta preserved)."""
        return self._manifest

    @property
    def reader(self) -> ArchiveReader:
        """The underlying reader (for blob access — blobs are never migrated)."""
        return self._reader

    def read_rows(self, table: str) -> list[dict[str, Any]]:
        """Return a table's rows at the current format version."""
        if self._migrated_rows is not None:
            return list(self._migrated_rows.get(table, []))
        return list(self._reader.read_rows(table))


def migrate_reader(reader: ArchiveReader) -> MigratedView:
    """Build a :class:`MigratedView` that serves rows at the current version.

    Validates the chain, rejects versions outside the supported window, and — if
    the archive is older — applies each ``vN -> vN+1`` transform in order.
    """
    _validate_chain()
    found = reader.manifest.format_version
    if found > CURRENT_FORMAT_VERSION or found < MIN_SUPPORTED_FORMAT_VERSION:
        raise UnsupportedArchiveVersionError(
            found, (MIN_SUPPORTED_FORMAT_VERSION, CURRENT_FORMAT_VERSION)
        )
    if found == CURRENT_FORMAT_VERSION:
        return MigratedView(reader, reader.manifest, None)

    # Buffer every table, then walk the chain.
    rows_by_table: dict[str, list[dict[str, Any]]] = {
        table: list(reader.read_rows(table)) for table in reader.tables()
    }
    manifest = reader.manifest
    version = found
    while version < CURRENT_FORMAT_VERSION:
        transform = _REGISTRY[version]
        rows_by_table = transform(manifest, rows_by_table)
        version += 1
        manifest = manifest.model_copy(update={"format_version": version})
    return MigratedView(reader, manifest, rows_by_table)


def registered_versions() -> list[int]:
    """The ``from_version`` keys currently registered (for tests/inspection)."""
    return sorted(_REGISTRY)


# --------------------------------------------------------------------------- #
# Registered migrations
# --------------------------------------------------------------------------- #
#
# v1 is the current version, so there are no real transforms yet. The chain
# machinery is fully exercised by tests via a temporary registration, and the
# first real schema change registers its ``@migration(from_version=1)`` here.

_validate_chain()


__all__ = [
    "MigratedView",
    "MigrationFn",
    "migrate_reader",
    "migration",
    "registered_versions",
]
