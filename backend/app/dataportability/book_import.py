"""Full book-bundle import — archive → DB rows (remapped) + restored blobs.

The inverse of :mod:`app.dataportability.book_export`. Importing a ``.kinora`` book
bundle into a (possibly different) deployment:

1. **Migrates** the archive to the current format version (see
   :mod:`app.dataportability.migrate`) so an old archive still loads.
2. **Mints** a fresh id for every primary key (two-phase, so forward references
   resolve) and **rewrites every reference** in lockstep — the imported graph is
   internally consistent and never collides with existing rows.
3. **Re-homes ownership**: the new book attaches to the importing user, and the
   archive's ``users`` rows are *not* re-created (the caller already exists).
4. **Rewrites object keys** so the book-id path segment points at the new book,
   and **restores blobs** to those keys.
5. Runs in **one unit of work** (the caller's session) and fails closed on any
   dangling reference, so a partial graph is never written.

The result is a `BookImportResult` naming the new book id + per-table counts.
"""

from __future__ import annotations

from typing import IO, Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.dataportability.blobs import BlobImporter
from app.dataportability.codec import ArchiveReader
from app.dataportability.dbio import BookWriter
from app.dataportability.errors import ArchiveKindMismatchError
from app.dataportability.idremap import IdRemapper
from app.dataportability.keys import KEY_REWRITERS
from app.dataportability.manifest import ArchiveKind
from app.dataportability.migrate import migrate_reader
from app.dataportability.scrub import ROW_SCRUBBERS
from app.dataportability.serialization import BOOK_SCOPED_TABLES
from app.memory.interfaces import BlobStore


class BookImportResult(BaseModel):
    """The outcome of importing one book bundle."""

    new_book_id: str
    old_book_id: str | None = None
    title: str | None = None
    table_counts: dict[str, int] = Field(default_factory=dict)
    blobs_restored: int = 0


class BookImporter:
    """Import a ``.kinora`` book bundle into the caller's session + object store."""

    def __init__(self, session: AsyncSession, *, blob_store: BlobStore) -> None:
        self._session = session
        self._writer = BookWriter(session)
        self._blobs = BlobImporter(blob_store)

    async def import_archive(
        self,
        reader: ArchiveReader,
        *,
        owner_user_id: str,
        verify: bool = True,
        restore_blobs: bool = True,
    ) -> BookImportResult:
        """Import the archive open on ``reader`` as a new book owned by the caller."""
        if verify:
            reader.verify()
        view = migrate_reader(reader)
        if view.manifest.kind != ArchiveKind.BOOK:
            raise ArchiveKindMismatchError(ArchiveKind.BOOK, view.manifest.kind)

        # Load every portable table's rows into memory (bounded per book) so the
        # two-phase remap can mint all PKs before rewriting any reference.
        rows_by_table: dict[str, list[dict[str, Any]]] = {}
        for table in BOOK_SCOPED_TABLES:
            rows_by_table[table] = list(view.read_rows(table))

        old_book_id = self._old_book_id(rows_by_table)

        remap = IdRemapper()
        # Pin the new book + owner so children attach correctly.
        if old_book_id is not None:
            new_book_id = remap.space("book").mint(old_book_id)
        else:  # pragma: no cover - a book bundle always has a books row
            new_book_id = remap.space("book").mint("__missing_book__")
        # Any user id referenced by the archive's rows re-homes to the caller.
        self._pin_users(rows_by_table, remap, owner_user_id)

        # Phase 1: mint every PK across all tables.
        for table, rows in rows_by_table.items():
            remap.mint_table(table, rows)

        # Phase 2: rewrite + key-rewrite + insert, parent-table-first.
        result = BookImportResult(
            new_book_id=new_book_id,
            old_book_id=old_book_id,
            title=view.manifest.meta.get("title"),
        )
        for table in BOOK_SCOPED_TABLES:
            if table == "users":  # never re-create users; owner already exists
                continue
            inserted = await self._import_table(
                table, rows_by_table[table], remap, old_book_id, new_book_id
            )
            if inserted:
                result.table_counts[table] = inserted

        if restore_blobs and old_book_id is not None:
            await self._blobs.restore_all(
                view.reader, old_book_id=old_book_id, new_book_id=new_book_id
            )
            result.blobs_restored = self._blobs.restored
        return result

    # -- helpers ------------------------------------------------------------- #

    @staticmethod
    def _old_book_id(rows_by_table: dict[str, list[dict[str, Any]]]) -> str | None:
        books = rows_by_table.get("books") or []
        if books:
            value = books[0].get("id")
            return value if isinstance(value, str) else None
        return None

    @staticmethod
    def _pin_users(
        rows_by_table: dict[str, list[dict[str, Any]]],
        remap: IdRemapper,
        owner_user_id: str,
    ) -> None:
        """Pin every user id seen in the archive to the importing owner.

        A book bundle carries no ``users`` rows (it is one book, not an account),
        but its ``books`` / ``sessions`` / ``prefs`` rows reference a ``user_id``.
        Re-homing them all to the caller makes the imported book owned by whoever
        ran the import — the only sensible owner on a fresh deployment.
        """
        for table in ("books", "sessions", "prefs", "users"):
            for row in rows_by_table.get(table, []):
                col = "id" if table == "users" else "user_id"
                uid = row.get(col)
                if isinstance(uid, str):
                    remap.force_user_id(uid, owner_user_id)

    async def _import_table(
        self,
        table: str,
        rows: list[dict[str, Any]],
        remap: IdRemapper,
        old_book_id: str | None,
        new_book_id: str,
    ) -> int:
        """Remap ids + rewrite object keys for a table's rows, then insert them."""
        if not rows:
            return 0
        key_rewriter = KEY_REWRITERS.get(table)
        scrubber = ROW_SCRUBBERS.get(table)
        prepared: list[dict[str, Any]] = []
        for row in rows:
            mapped = remap.rewrite_row(table, row)
            if key_rewriter is not None and old_book_id is not None:
                key_rewriter(mapped, old_book_id, new_book_id)
            if scrubber is not None:
                scrubber(mapped, new_book_id=new_book_id)
            prepared.append(mapped)
        return await self._writer.insert_rows(table, prepared)


async def import_book_from_stream(
    session: AsyncSession,
    stream: IO[bytes],
    *,
    owner_user_id: str,
    blob_store: BlobStore,
    restore_blobs: bool = True,
) -> BookImportResult:
    """Convenience wrapper: import a book bundle from a binary stream."""
    with ArchiveReader(stream) as reader:
        return await BookImporter(session, blob_store=blob_store).import_archive(
            reader, owner_user_id=owner_user_id, restore_blobs=restore_blobs
        )


__all__ = ["BookImporter", "BookImportResult", "import_book_from_stream"]
