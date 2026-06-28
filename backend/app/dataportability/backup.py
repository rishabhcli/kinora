"""Backup + point-in-time restore orchestration.

A **backup** is a ``kind=backup`` ``.kinora`` archive that snapshots a chosen set
of books (their full bundles + blobs) at a moment in time, persisted to object
storage under ``backups/<snapshot_id>.kinora`` and tracked in a small JSON
**catalog** (``backups/catalog.json``). The catalog is the point-in-time index: a
caller lists snapshots, inspects one (manifest + verification), restores it (re-
import every book it holds, id-remapped, under a chosen owner), or prunes old
snapshots by age/count.

This avoids any dependency on S3 ``ListObjects`` (the object-store seam exposes
only get/put/exists/delete): the catalog *is* the listing, updated atomically on
each create/prune by read-modify-write of the one catalog object.

A snapshot id is time-ordered (``<utc-iso-compact>-<rand>``) so the catalog reads
back newest-first by id, and "restore the latest" / "restore as of <time>" both
reduce to picking a catalog entry.
"""

from __future__ import annotations

import io
import json
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import anyio
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.dataportability.account import AccountImporter
from app.dataportability.blobs import BlobExporter, DeletableBlobStore
from app.dataportability.codec import ArchiveReader, ArchiveWriter
from app.dataportability.dbio import BookReader
from app.dataportability.keys import (
    deterministic_book_keys,
    keys_from_book,
    keys_from_entity,
    keys_from_page,
    keys_from_shot,
    keys_from_shot_cache,
)
from app.dataportability.manifest import ArchiveKind, ArchiveManifest
from app.dataportability.serialization import BOOK_SCOPED_TABLES, RowCodec

#: Object-store layout for backups.
BACKUP_PREFIX = "backups/"
CATALOG_KEY = "backups/catalog.json"

_KEY_EXTRACTORS = {
    "books": keys_from_book,
    "pages": keys_from_page,
    "entities": keys_from_entity,
    "shots": keys_from_shot,
    "shot_cache": keys_from_shot_cache,
}


class SnapshotEntry(BaseModel):
    """One catalog entry describing a stored backup snapshot."""

    snapshot_id: str
    created_at: str
    label: str | None = None
    book_ids: list[str] = Field(default_factory=list)
    object_key: str
    size_bytes: int = 0
    manifest_digest: str = ""


class BackupCatalog(BaseModel):
    """The point-in-time index of all stored snapshots (newest-first)."""

    snapshots: list[SnapshotEntry] = Field(default_factory=list)

    def newest(self) -> SnapshotEntry | None:
        return self.snapshots[0] if self.snapshots else None

    def find(self, snapshot_id: str) -> SnapshotEntry | None:
        for entry in self.snapshots:
            if entry.snapshot_id == snapshot_id:
                return entry
        return None


class RestoreResult(BaseModel):
    """The outcome of restoring a snapshot."""

    snapshot_id: str
    restored_book_ids: list[str] = Field(default_factory=list)
    blobs_restored: int = 0


def _new_snapshot_id() -> str:
    """A time-ordered, unique snapshot id (sorts newest-last lexicographically)."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    return f"{stamp}-{secrets.token_hex(4)}"


def _backup_key(snapshot_id: str) -> str:
    return f"{BACKUP_PREFIX}{snapshot_id}.kinora"


class BackupOrchestrator:
    """Create, list, inspect, restore, and prune backup snapshots."""

    def __init__(self, session: AsyncSession, *, blob_store: DeletableBlobStore) -> None:
        self._session = session
        self._store = blob_store
        self._reader = BookReader(session)
        self._blobs = BlobExporter(blob_store)

    # -- catalog ------------------------------------------------------------- #

    async def load_catalog(self) -> BackupCatalog:
        """Read the backup catalog (empty if none exists yet)."""
        exists = await anyio.to_thread.run_sync(self._store.exists, CATALOG_KEY)
        if not exists:
            return BackupCatalog()
        raw = await anyio.to_thread.run_sync(self._store.get_bytes, CATALOG_KEY)
        try:
            return BackupCatalog.model_validate_json(raw)
        except Exception:  # noqa: BLE001 - a corrupt catalog starts fresh, snapshots persist
            return BackupCatalog()

    async def _save_catalog(self, catalog: BackupCatalog) -> None:
        # Keep newest-first.
        catalog.snapshots.sort(key=lambda s: s.snapshot_id, reverse=True)
        payload = catalog.model_dump_json(indent=2).encode("utf-8")
        await anyio.to_thread.run_sync(
            self._store.put_bytes, CATALOG_KEY, payload, "application/json"
        )

    # -- create -------------------------------------------------------------- #

    async def create(
        self,
        book_ids: Sequence[str],
        *,
        label: str | None = None,
        include_blobs: bool = True,
    ) -> SnapshotEntry:
        """Snapshot ``book_ids`` to a backup archive in object storage + catalog."""
        snapshot_id = _new_snapshot_id()
        manifest = ArchiveManifest(
            kind=ArchiveKind.BACKUP,
            meta={
                "snapshot_id": snapshot_id,
                "label": label,
                "book_ids": list(book_ids),
            },
        )
        buffer = io.BytesIO()
        referenced_keys: set[str] = set()
        per_table: dict[str, list[dict[str, Any]]] = {t: [] for t in BOOK_SCOPED_TABLES}
        for book_id in book_ids:
            from app.db.models.book import Book

            book = await self._session.get(Book, book_id)
            page_count = getattr(book, "num_pages", None) if book is not None else None
            referenced_keys.update(deterministic_book_keys(book_id, page_count=page_count))
            for table in BOOK_SCOPED_TABLES:
                if table == "users":
                    continue
                extractor = _KEY_EXTRACTORS.get(table)
                async for row in self._reader.stream_table(table, book_id):
                    if extractor is not None:
                        referenced_keys.update(extractor(row))
                    per_table[table].append(row)

        with ArchiveWriter(buffer, manifest) as writer:
            await self._write_owner_rows(writer, book_ids)
            for table in BOOK_SCOPED_TABLES:
                if table == "users":
                    continue
                writer.write_rows(table, per_table[table])
            if include_blobs:
                await self._blobs.export_keys(writer, sorted(referenced_keys))
            writer.update_meta({"blobs_exported": self._blobs.exported})

        payload = buffer.getvalue()
        object_key = _backup_key(snapshot_id)
        await anyio.to_thread.run_sync(
            self._store.put_bytes, object_key, payload, "application/zip"
        )
        sealed = manifest.sealed()
        entry = SnapshotEntry(
            snapshot_id=snapshot_id,
            created_at=manifest.created_at,
            label=label,
            book_ids=list(book_ids),
            object_key=object_key,
            size_bytes=len(payload),
            manifest_digest=sealed.manifest_digest,
        )
        catalog = await self.load_catalog()
        catalog.snapshots.append(entry)
        await self._save_catalog(catalog)
        return entry

    async def _write_owner_rows(self, writer: ArchiveWriter, book_ids: Sequence[str]) -> None:
        """Write the distinct owner ``users`` rows for the snapshot's books.

        A backup is account-shaped, so it can carry the owning user rows; the
        password hash is redacted (a backup is not an auth dump).
        """
        from app.db.models.book import Book
        from app.db.models.user import User

        seen: set[str] = set()
        user_rows: list[dict[str, Any]] = []
        codec = RowCodec(User)
        for book_id in book_ids:
            book = await self._session.get(Book, book_id)
            uid = getattr(book, "user_id", None) if book is not None else None
            if isinstance(uid, str) and uid not in seen:
                seen.add(uid)
                user = await self._session.get(User, uid)
                if user is not None:
                    row = codec.to_dict(user)
                    row["hashed_password"] = ""
                    user_rows.append(row)
        if user_rows:
            writer.write_rows("users", user_rows)

    # -- inspect / verify ---------------------------------------------------- #

    async def inspect(self, snapshot_id: str) -> ArchiveManifest | None:
        """Return a snapshot's manifest (after a full integrity verify), or None."""
        catalog = await self.load_catalog()
        entry = catalog.find(snapshot_id)
        if entry is None:
            return None
        raw = await anyio.to_thread.run_sync(self._store.get_bytes, entry.object_key)
        with ArchiveReader(io.BytesIO(raw)) as reader:
            reader.verify()
            return reader.manifest

    # -- restore ------------------------------------------------------------- #

    async def restore(
        self,
        snapshot_id: str,
        *,
        owner_user_id: str,
        restore_blobs: bool = True,
    ) -> RestoreResult:
        """Restore a snapshot: re-import every book it holds under ``owner_user_id``."""
        catalog = await self.load_catalog()
        entry = catalog.find(snapshot_id)
        if entry is None:
            raise FileNotFoundError(f"no such snapshot {snapshot_id!r}")
        raw = await anyio.to_thread.run_sync(self._store.get_bytes, entry.object_key)
        with ArchiveReader(io.BytesIO(raw)) as reader:
            # A backup is account-shaped (identical row layout), so the account
            # importer handles it directly once told to accept the BACKUP kind.
            importer = AccountImporter(self._session, blob_store=self._store)
            account_result = await importer.import_archive(
                reader,
                owner_user_id=owner_user_id,
                restore_blobs=restore_blobs,
                accept_kinds=(ArchiveKind.BACKUP, ArchiveKind.ACCOUNT),
            )
        return RestoreResult(
            snapshot_id=snapshot_id,
            restored_book_ids=account_result.book_ids,
            blobs_restored=account_result.blobs_restored,
        )

    # -- prune --------------------------------------------------------------- #

    async def prune(self, *, keep_last: int | None = None) -> list[str]:
        """Delete snapshots beyond the ``keep_last`` newest; return removed ids."""
        catalog = await self.load_catalog()
        catalog.snapshots.sort(key=lambda s: s.snapshot_id, reverse=True)
        if keep_last is None or keep_last < 0:
            return []
        to_remove = catalog.snapshots[keep_last:]
        kept = catalog.snapshots[:keep_last]
        for entry in to_remove:
            await anyio.to_thread.run_sync(self._store.delete, entry.object_key)
        catalog.snapshots = kept
        await self._save_catalog(catalog)
        return [e.snapshot_id for e in to_remove]


def _catalog_roundtrip(data: bytes) -> BackupCatalog:
    """Parse a catalog payload (used by tests / inspection)."""
    return BackupCatalog.model_validate(json.loads(data))


__all__ = [
    "BACKUP_PREFIX",
    "CATALOG_KEY",
    "BackupCatalog",
    "BackupOrchestrator",
    "RestoreResult",
    "SnapshotEntry",
]
