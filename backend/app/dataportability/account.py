"""GDPR account data export + right-to-erasure.

Two regulator-facing operations over *all* of a user's data:

* **Export** (GDPR Art. 20, data portability) — a single ``kind=account``
  ``.kinora`` archive holding the user's profile row (email; the password hash is
  redacted by default) and the full bundle of **every book they own** (all
  portable tables across all their books, plus every referenced blob). Because
  the archive stores rows under shared flat table names keyed by their existing
  globally-unique ids, one account archive losslessly carries N books at once;
  import id-remaps the whole graph in one pass.

* **Erasure** (GDPR Art. 17, right to be forgotten) — a cascade-aware hard delete
  of the user and everything they own: every owned book (whose ``ON DELETE
  CASCADE`` FKs sweep its pages/scenes/beats/canon/shots/sessions/etc.), every
  object-store blob those books reference, and finally the ``users`` row. A
  **dry-run** returns the plan (counts + blob keys) without deleting anything, so
  a confirmation UI can show exactly what will be removed.

Both run in the caller's session; export is read-only, erasure commits via the
caller's unit of work.
"""

from __future__ import annotations

from typing import IO, Any

from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dataportability.blobs import BlobExporter, BlobImporter, DeletableBlobStore
from app.dataportability.codec import ArchiveReader, ArchiveWriter
from app.dataportability.dbio import BookReader, BookWriter
from app.dataportability.errors import ArchiveKindMismatchError
from app.dataportability.idremap import IdRemapper
from app.dataportability.keys import (
    KEY_REWRITERS,
    collect_book_keys,
    deterministic_book_keys,
    keys_from_book,
    keys_from_entity,
    keys_from_page,
    keys_from_shot,
    keys_from_shot_cache,
)
from app.dataportability.manifest import ArchiveKind, ArchiveManifest
from app.dataportability.migrate import migrate_reader
from app.dataportability.scrub import ROW_SCRUBBERS
from app.dataportability.serialization import BOOK_SCOPED_TABLES, RowCodec
from app.memory.interfaces import BlobStore

#: Tables whose rows carry object keys (for blob collection during account export).
_KEY_EXTRACTORS = {
    "books": keys_from_book,
    "pages": keys_from_page,
    "entities": keys_from_entity,
    "shots": keys_from_shot,
    "shot_cache": keys_from_shot_cache,
}


class ErasurePlan(BaseModel):
    """What a right-to-erasure would delete (a dry-run, or the executed plan)."""

    user_id: str
    book_ids: list[str] = Field(default_factory=list)
    row_counts: dict[str, int] = Field(default_factory=dict)
    blob_keys: list[str] = Field(default_factory=list)
    executed: bool = False


class AccountImportResult(BaseModel):
    """The outcome of importing an account archive."""

    user_id: str
    book_ids: list[str] = Field(default_factory=list)
    table_counts: dict[str, int] = Field(default_factory=dict)
    blobs_restored: int = 0


class AccountExporter:
    """Export everything a user owns into one ``kind=account`` archive."""

    def __init__(self, session: AsyncSession, *, blob_store: BlobStore) -> None:
        self._session = session
        self._reader = BookReader(session)
        self._blobs = BlobExporter(blob_store)

    async def _owned_book_ids(self, user_id: str) -> list[str]:
        from app.db.models.book import Book

        rows = await self._session.execute(
            select(Book.id).where(Book.user_id == user_id).order_by(Book.created_at)
        )
        return [r[0] for r in rows.all()]

    async def export(
        self,
        user_id: str,
        stream: IO[bytes],
        *,
        include_blobs: bool = True,
        redact_password: bool = True,
    ) -> ArchiveManifest:
        """Write the account archive to ``stream``; return the sealed manifest."""
        from app.db.models.user import User

        user = await self._session.get(User, user_id)
        book_ids = await self._owned_book_ids(user_id)
        manifest = ArchiveManifest(
            kind=ArchiveKind.ACCOUNT,
            meta={
                "user_id": user_id,
                "email": getattr(user, "email", None),
                "book_ids": book_ids,
            },
        )
        referenced_keys: set[str] = set()
        # Aggregate every book's rows under the shared flat table names.
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

        with ArchiveWriter(stream, manifest) as writer:
            # The user profile row (password hash redacted unless asked otherwise).
            if user is not None:
                user_row = RowCodec(User).to_dict(user)
                if redact_password:
                    user_row["hashed_password"] = ""
                writer.write_rows("users", [user_row])
            for table in BOOK_SCOPED_TABLES:
                if table == "users":
                    continue
                writer.write_rows(table, per_table[table])
            if include_blobs:
                await self._blobs.export_keys(writer, sorted(referenced_keys))
            blob_meta = {
                "blobs_exported": self._blobs.exported,
                "blobs_missing": list(self._blobs.missing),
            }
            manifest.meta.update(blob_meta)
            writer.update_meta(blob_meta)
        return manifest.sealed()


class AccountImporter:
    """Import an account archive: re-create books under a (possibly new) owner."""

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
        accept_kinds: tuple[str, ...] = (ArchiveKind.ACCOUNT,),
    ) -> AccountImportResult:
        """Import every book in an account-shaped archive under the caller.

        ``accept_kinds`` widens the accepted manifest kind: a backup archive has
        the identical row layout, so the backup restore path passes
        ``(ArchiveKind.ACCOUNT, ArchiveKind.BACKUP)`` instead of re-tagging.
        """
        if verify:
            reader.verify()
        view = migrate_reader(reader)
        if view.manifest.kind not in accept_kinds:
            raise ArchiveKindMismatchError(accept_kinds[0], view.manifest.kind)

        rows_by_table = {t: list(view.read_rows(t)) for t in BOOK_SCOPED_TABLES}
        old_to_new_book: dict[str, str] = {}

        remap = IdRemapper()
        # Re-home every user reference to the importing owner.
        for table in ("users", "books", "sessions", "prefs"):
            for row in rows_by_table.get(table, []):
                col = "id" if table == "users" else "user_id"
                uid = row.get(col)
                if isinstance(uid, str):
                    remap.force_user_id(uid, owner_user_id)
        # Mint book ids first so we can report old->new.
        for row in rows_by_table.get("books", []):
            old = row.get("id")
            if isinstance(old, str):
                old_to_new_book[old] = remap.space("book").mint(old)
        # Phase 1: mint all remaining PKs.
        for table, rows in rows_by_table.items():
            remap.mint_table(table, rows)

        result = AccountImportResult(
            user_id=owner_user_id, book_ids=list(old_to_new_book.values())
        )
        for table in BOOK_SCOPED_TABLES:
            if table == "users":
                continue
            rows = rows_by_table[table]
            if not rows:
                continue
            key_rewriter = KEY_REWRITERS.get(table)
            scrubber = ROW_SCRUBBERS.get(table)
            prepared: list[dict[str, Any]] = []
            for row in rows:
                mapped = remap.rewrite_row(table, row)
                old_bid = self._row_old_book_id(table, row, old_to_new_book)
                new_bid = old_to_new_book.get(old_bid) if old_bid is not None else None
                if key_rewriter is not None and old_bid is not None and new_bid is not None:
                    key_rewriter(mapped, old_bid, new_bid)
                if scrubber is not None and new_bid is not None:
                    scrubber(mapped, new_book_id=new_bid)
                prepared.append(mapped)
            inserted = await self._writer.insert_rows(table, prepared)
            result.table_counts[table] = result.table_counts.get(table, 0) + inserted

        if restore_blobs:
            for old_bid, new_bid in old_to_new_book.items():
                await self._blobs.restore_all(
                    view.reader, old_book_id=old_bid, new_book_id=new_bid
                )
            result.blobs_restored = self._blobs.restored
        return result

    @staticmethod
    def _row_old_book_id(
        table: str, row: dict[str, Any], old_to_new_book: dict[str, str]
    ) -> str | None:
        """The original book id a key-bearing row belongs to (pre-remap)."""
        bid = row.get("id") if table == "books" else row.get("book_id")
        return bid if isinstance(bid, str) and bid in old_to_new_book else None


class AccountEraser:
    """Right-to-erasure: delete a user and everything they own (dry-run aware)."""

    def __init__(self, session: AsyncSession, *, blob_store: DeletableBlobStore) -> None:
        self._session = session
        self._reader = BookReader(session)
        self._store = blob_store

    async def plan(self, user_id: str) -> ErasurePlan:
        """Compute the erasure plan (counts + blob keys) without deleting anything."""
        from app.db.models.book import Book

        rows = await self._session.execute(select(Book.id).where(Book.user_id == user_id))
        book_ids = [r[0] for r in rows.all()]
        plan = ErasurePlan(user_id=user_id, book_ids=book_ids)
        all_keys: set[str] = set()
        for book_id in book_ids:
            book = await self._session.get(Book, book_id)
            page_count = getattr(book, "num_pages", None) if book is not None else None
            collected: dict[str, list[dict[str, Any]]] = {}
            for table in ("books", "pages", "entities", "shots", "shot_cache"):
                collected[table] = [
                    row async for row in self._reader.stream_table(table, book_id)
                ]
            book_row = collected["books"][0] if collected["books"] else None
            all_keys |= collect_book_keys(
                book_id,
                page_count=page_count,
                book_row=book_row,
                pages=collected["pages"],
                entities=collected["entities"],
                shots=collected["shots"],
                shot_cache=collected["shot_cache"],
            )
            # Also the deterministic vault key prefix isn't enumerable cheaply; the
            # per-book deterministic keys cover the source doc + cover + pages.
            all_keys |= set(deterministic_book_keys(book_id, page_count=page_count))
            for table in BOOK_SCOPED_TABLES:
                if table == "users":
                    continue
                plan.row_counts[table] = plan.row_counts.get(table, 0) + (
                    await self._reader.count_table(table, book_id)
                )
        plan.blob_keys = sorted(all_keys)
        return plan

    async def erase(self, user_id: str, *, purge_blobs: bool = True) -> ErasurePlan:
        """Execute erasure: purge blobs, cascade-delete books, delete the user."""
        import anyio

        from app.db.models.book import Book
        from app.db.models.user import User

        plan = await self.plan(user_id)
        if purge_blobs:
            for key in plan.blob_keys:
                # delete is idempotent (no error if absent), so a missing asset
                # (e.g. an unrendered clip) is silently fine.
                await anyio.to_thread.run_sync(self._store.delete, key)
        # Deleting the books cascades to all book-scoped rows (ON DELETE CASCADE).
        for book_id in plan.book_ids:
            await self._session.execute(delete(Book).where(Book.id == book_id))
        await self._session.execute(delete(User).where(User.id == user_id))
        await self._session.flush()
        plan.executed = True
        return plan


async def export_account_to_stream(
    session: AsyncSession,
    user_id: str,
    stream: IO[bytes],
    *,
    blob_store: BlobStore,
    include_blobs: bool = True,
) -> ArchiveManifest:
    """Convenience wrapper: GDPR-export ``user_id`` to ``stream``."""
    return await AccountExporter(session, blob_store=blob_store).export(
        user_id, stream, include_blobs=include_blobs
    )


__all__ = [
    "AccountEraser",
    "AccountExporter",
    "AccountImportResult",
    "AccountImporter",
    "ErasurePlan",
    "export_account_to_stream",
]
