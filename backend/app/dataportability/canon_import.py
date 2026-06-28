"""Canon-graph import — merge or replace a book's §8 canon from an archive.

Imports a ``kind=canon_graph`` archive into an **existing target book**. The
target book id is supplied by the caller and pinned in the id remapper, so every
canon row's ``book_id`` re-homes to the target while its row PKs are minted fresh
and intra-archive references (``entities.supersedes``) are preserved.

Two modes:

* ``replace`` (default) — delete the target book's existing canon rows first, so
  the imported canon becomes the book's canon (the "restore a bible" use case);
* ``merge`` — insert alongside the existing canon (the "graft another story's
  canon in" use case). The caller owns the consequences of overlapping
  ``entity_key`` version chains.

Object keys in the entity rows are rewritten from the archive's source book id to
the target book id, and reference assets are restored to the target's key space.
Runs in the caller's unit of work; fails closed on a dangling reference.
"""

from __future__ import annotations

from typing import IO, Any

from pydantic import BaseModel, Field
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.dataportability.blobs import BlobImporter
from app.dataportability.codec import ArchiveReader
from app.dataportability.dbio import BookWriter
from app.dataportability.errors import ArchiveKindMismatchError
from app.dataportability.idremap import IdRemapper
from app.dataportability.keys import KEY_REWRITERS
from app.dataportability.manifest import ArchiveKind
from app.dataportability.migrate import migrate_reader
from app.dataportability.serialization import CANON_TABLES, table_registry
from app.memory.interfaces import BlobStore


class CanonImportResult(BaseModel):
    """The outcome of importing a canon graph into a target book."""

    target_book_id: str
    source_book_id: str | None = None
    mode: str = "replace"
    table_counts: dict[str, int] = Field(default_factory=dict)
    blobs_restored: int = 0


class CanonImporter:
    """Import a canon archive into an existing book (merge or replace)."""

    def __init__(self, session: AsyncSession, *, blob_store: BlobStore) -> None:
        self._session = session
        self._writer = BookWriter(session)
        self._blobs = BlobImporter(blob_store)

    async def import_archive(
        self,
        reader: ArchiveReader,
        *,
        target_book_id: str,
        mode: str = "replace",
        verify: bool = True,
        restore_blobs: bool = True,
    ) -> CanonImportResult:
        """Import the canon archive into ``target_book_id`` (``replace``/``merge``)."""
        if mode not in {"replace", "merge"}:
            raise ValueError("mode must be 'replace' or 'merge'")
        if verify:
            reader.verify()
        view = migrate_reader(reader)
        if view.manifest.kind != ArchiveKind.CANON:
            raise ArchiveKindMismatchError(ArchiveKind.CANON, view.manifest.kind)

        source_book_id = view.manifest.meta.get("source_book_id")
        rows_by_table = {table: list(view.read_rows(table)) for table in CANON_TABLES}

        if mode == "replace":
            await self._delete_existing_canon(target_book_id)

        remap = IdRemapper()
        remap.force_book_id(source_book_id or "__src__", target_book_id)
        for table, rows in rows_by_table.items():
            remap.mint_table(table, rows)

        result = CanonImportResult(
            target_book_id=target_book_id, source_book_id=source_book_id, mode=mode
        )
        for table in CANON_TABLES:
            rows = rows_by_table[table]
            if not rows:
                continue
            key_rewriter = KEY_REWRITERS.get(table)
            prepared: list[dict[str, Any]] = []
            for row in rows:
                mapped = remap.rewrite_row(table, row)
                if key_rewriter is not None and source_book_id:
                    key_rewriter(mapped, source_book_id, target_book_id)
                prepared.append(mapped)
            inserted = await self._writer.insert_rows(table, prepared)
            result.table_counts[table] = inserted

        if restore_blobs and source_book_id:
            await self._blobs.restore_all(
                view.reader, old_book_id=source_book_id, new_book_id=target_book_id
            )
            result.blobs_restored = self._blobs.restored
        return result

    async def _delete_existing_canon(self, book_id: str) -> None:
        """Delete the target book's existing canon rows (replace mode)."""
        registry = table_registry()
        for table in CANON_TABLES:
            model = registry[table]
            book_col = model.__mapper__.columns["book_id"]
            await self._session.execute(delete(model).where(book_col == book_id))
        await self._session.flush()


async def import_canon_from_stream(
    session: AsyncSession,
    stream: IO[bytes],
    *,
    target_book_id: str,
    blob_store: BlobStore,
    mode: str = "replace",
    restore_blobs: bool = True,
) -> CanonImportResult:
    """Convenience wrapper: import a canon graph into ``target_book_id``."""
    with ArchiveReader(stream) as reader:
        return await CanonImporter(session, blob_store=blob_store).import_archive(
            reader, target_book_id=target_book_id, mode=mode, restore_blobs=restore_blobs
        )


__all__ = ["CanonImporter", "CanonImportResult", "import_canon_from_stream"]
