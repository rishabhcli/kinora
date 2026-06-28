"""PortabilityService — the façade the HTTP route (and tests) call.

Wires the package's exporters/importers/orchestrators to a session factory + an
object store (the two seams the :class:`app.composition.Container` already owns)
and exposes one coherent surface:

* ``export_book`` / ``import_book``         — full book bundle;
* ``export_canon`` / ``import_canon``       — canon graph only;
* ``export_account`` / ``erase_account``    — GDPR export + right-to-erasure;
* ``create_backup`` / ``list_backups`` / ``inspect_backup`` / ``restore_backup``
  / ``prune_backups``                        — backup + point-in-time restore;
* ``inspect_archive``                        — verify + summarize an uploaded archive.

Each operation runs in its **own** unit of work (the factory's committing
context), so an import either fully commits or fully rolls back. Exports return
the archive as bytes (the route streams them); imports take bytes.

Ownership is enforced *above* this layer (the route checks ``books.user_id`` like
the rest of the API); the service trusts its caller's ``user_id`` for re-homing.
"""

from __future__ import annotations

import io
from collections.abc import Sequence

from app.composition import SessionFactory
from app.dataportability.account import (
    AccountEraser,
    AccountExporter,
    AccountImporter,
    AccountImportResult,
    ErasurePlan,
)
from app.dataportability.backup import (
    BackupCatalog,
    BackupOrchestrator,
    RestoreResult,
    SnapshotEntry,
)
from app.dataportability.blobs import DeletableBlobStore
from app.dataportability.book_export import BookExporter
from app.dataportability.book_import import BookImporter, BookImportResult
from app.dataportability.canon_export import CanonExporter
from app.dataportability.canon_import import CanonImporter, CanonImportResult
from app.dataportability.codec import ArchiveReader
from app.dataportability.manifest import ArchiveManifest


class ArchiveInspection(ArchiveManifest):
    """An inspected archive: its manifest plus a verification verdict + tables."""

    verified: bool = False
    verify_error: str | None = None
    tables: list[str] = []  # noqa: RUF012 - pydantic field default


class PortabilityService:
    """High-level export/import/backup operations over one DB + object store."""

    def __init__(self, session_factory: SessionFactory, store: DeletableBlobStore) -> None:
        self._sf = session_factory
        self._store = store

    # -- book bundle --------------------------------------------------------- #

    async def export_book(self, book_id: str, *, include_blobs: bool = True) -> bytes:
        """Export a full book bundle; return the ``.kinora`` archive bytes."""
        buffer = io.BytesIO()
        async with self._sf() as session:
            await BookExporter(session, blob_store=self._store).export(
                book_id, buffer, include_blobs=include_blobs
            )
        return buffer.getvalue()

    async def import_book(
        self, data: bytes, *, owner_user_id: str, restore_blobs: bool = True
    ) -> BookImportResult:
        """Import a book bundle as a new book owned by ``owner_user_id``."""
        async with self._sf() as session:
            with ArchiveReader(io.BytesIO(data)) as reader:
                return await BookImporter(session, blob_store=self._store).import_archive(
                    reader, owner_user_id=owner_user_id, restore_blobs=restore_blobs
                )

    # -- canon graph --------------------------------------------------------- #

    async def export_canon(self, book_id: str, *, include_blobs: bool = True) -> bytes:
        """Export a book's canon graph (only); return archive bytes."""
        buffer = io.BytesIO()
        async with self._sf() as session:
            await CanonExporter(session, blob_store=self._store).export(
                book_id, buffer, include_blobs=include_blobs
            )
        return buffer.getvalue()

    async def import_canon(
        self,
        data: bytes,
        *,
        target_book_id: str,
        mode: str = "replace",
        restore_blobs: bool = True,
    ) -> CanonImportResult:
        """Import a canon graph into ``target_book_id`` (``replace`` or ``merge``)."""
        async with self._sf() as session:
            with ArchiveReader(io.BytesIO(data)) as reader:
                return await CanonImporter(session, blob_store=self._store).import_archive(
                    reader,
                    target_book_id=target_book_id,
                    mode=mode,
                    restore_blobs=restore_blobs,
                )

    # -- account / GDPR ------------------------------------------------------ #

    async def export_account(self, user_id: str, *, include_blobs: bool = True) -> bytes:
        """GDPR data-portability export of everything ``user_id`` owns."""
        buffer = io.BytesIO()
        async with self._sf() as session:
            await AccountExporter(session, blob_store=self._store).export(
                user_id, buffer, include_blobs=include_blobs
            )
        return buffer.getvalue()

    async def import_account(
        self, data: bytes, *, owner_user_id: str, restore_blobs: bool = True
    ) -> AccountImportResult:
        """Import an account archive as books owned by ``owner_user_id``."""
        async with self._sf() as session:
            with ArchiveReader(io.BytesIO(data)) as reader:
                return await AccountImporter(session, blob_store=self._store).import_archive(
                    reader, owner_user_id=owner_user_id, restore_blobs=restore_blobs
                )

    async def erasure_plan(self, user_id: str) -> ErasurePlan:
        """Dry-run right-to-erasure: what would be deleted, deleting nothing."""
        async with self._sf() as session:
            return await AccountEraser(session, blob_store=self._store).plan(user_id)

    async def erase_account(self, user_id: str, *, purge_blobs: bool = True) -> ErasurePlan:
        """Execute right-to-erasure (cascade delete + blob purge)."""
        async with self._sf() as session:
            return await AccountEraser(session, blob_store=self._store).erase(
                user_id, purge_blobs=purge_blobs
            )

    # -- backup / restore ---------------------------------------------------- #

    async def create_backup(
        self, book_ids: Sequence[str], *, label: str | None = None
    ) -> SnapshotEntry:
        """Snapshot a set of books to a stored backup archive + catalog entry."""
        async with self._sf() as session:
            return await BackupOrchestrator(session, blob_store=self._store).create(
                book_ids, label=label
            )

    async def list_backups(self) -> BackupCatalog:
        """List all stored backup snapshots (newest-first)."""
        async with self._sf() as session:
            return await BackupOrchestrator(session, blob_store=self._store).load_catalog()

    async def inspect_backup(self, snapshot_id: str) -> ArchiveManifest | None:
        """Verify + return a stored snapshot's manifest (None if unknown)."""
        async with self._sf() as session:
            return await BackupOrchestrator(session, blob_store=self._store).inspect(
                snapshot_id
            )

    async def restore_backup(
        self, snapshot_id: str, *, owner_user_id: str, restore_blobs: bool = True
    ) -> RestoreResult:
        """Restore a snapshot's books under ``owner_user_id``."""
        async with self._sf() as session:
            return await BackupOrchestrator(session, blob_store=self._store).restore(
                snapshot_id, owner_user_id=owner_user_id, restore_blobs=restore_blobs
            )

    async def prune_backups(self, *, keep_last: int) -> list[str]:
        """Delete snapshots beyond the newest ``keep_last``; return removed ids."""
        async with self._sf() as session:
            return await BackupOrchestrator(session, blob_store=self._store).prune(
                keep_last=keep_last
            )

    # -- inspection ---------------------------------------------------------- #

    @staticmethod
    def inspect_archive(data: bytes) -> ArchiveInspection:
        """Verify + summarize an uploaded archive without importing it.

        Never raises on a bad archive: a structural/checksum failure is reported
        in ``verified=False`` + ``verify_error`` so the route can return a 200 with
        the verdict (the caller decides whether to proceed to import).
        """
        try:
            with ArchiveReader(io.BytesIO(data)) as reader:
                manifest = reader.manifest
                tables = reader.tables()
                try:
                    reader.verify()
                    verified, err = True, None
                except Exception as exc:  # noqa: BLE001 - report, never raise
                    verified, err = False, str(exc)
        except Exception as exc:  # noqa: BLE001 - unreadable archive
            return ArchiveInspection(verified=False, verify_error=str(exc))
        return ArchiveInspection(
            **manifest.model_dump(), verified=verified, verify_error=err, tables=tables
        )


__all__ = ["ArchiveInspection", "PortabilityService"]
