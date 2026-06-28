"""Full book-bundle export — DB rows + referenced object-store blobs → archive.

Produces a ``kind=book_bundle`` ``.kinora`` archive containing every row of a
book across all portable tables (source span index, canon graph, shots, sync
maps, films metadata, sessions, budget ledger, defects, prefs) **and** every
object-store payload those rows reference (source PDF/EPUB, cover, page images,
keyframes, clips, last-frames, narration audio, reference assets, canon vault).

The export is the inverse of :mod:`app.dataportability.book_import`: round-tripping
an archive through export → import reproduces the book's full graph (with new
ids) and byte-identical blobs.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import IO, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.dataportability.blobs import BlobExporter
from app.dataportability.codec import ArchiveWriter
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
from app.dataportability.serialization import BOOK_SCOPED_TABLES
from app.memory.interfaces import BlobStore

#: Tables whose rows reference object keys; their key-extractor (so the exporter
#: knows which blobs to pull without re-querying).
_KEY_EXTRACTORS = {
    "books": keys_from_book,
    "pages": keys_from_page,
    "entities": keys_from_entity,
    "shots": keys_from_shot,
    "shot_cache": keys_from_shot_cache,
}


class BookExporter:
    """Export one book to a ``.kinora`` archive on the given binary stream."""

    def __init__(self, session: AsyncSession, *, blob_store: BlobStore) -> None:
        self._reader = BookReader(session)
        self._blobs = BlobExporter(blob_store)
        self._session = session

    async def export(
        self,
        book_id: str,
        stream: IO[bytes],
        *,
        tables: Sequence[str] = BOOK_SCOPED_TABLES,
        include_blobs: bool = True,
        extra_meta: dict[str, Any] | None = None,
    ) -> ArchiveManifest:
        """Write the book bundle to ``stream``; return the sealed manifest.

        Streams each table's rows into the archive as it reads them (bounded
        memory), accumulating only the *object keys* (small strings) referenced by
        key-bearing tables. After the data members are written, the referenced
        blobs are pulled from object storage and appended (content-addressed,
        deduped). ``tables`` lets a caller export a subset (the backup path passes
        the full set; canon export uses a different module).
        """
        from app.db.models.book import Book

        book = await self._session.get(Book, book_id)
        page_count = getattr(book, "num_pages", None) if book is not None else None
        title = getattr(book, "title", None) if book is not None else None

        manifest = ArchiveManifest(
            kind=ArchiveKind.BOOK,
            meta={
                "book_id": book_id,
                "title": title,
                "page_count": page_count,
                **(extra_meta or {}),
            },
        )
        referenced_keys: set[str] = set()

        with ArchiveWriter(stream, manifest) as writer:
            for table in tables:
                extractor = _KEY_EXTRACTORS.get(table)
                count = await self._write_table(writer, table, book_id, extractor, referenced_keys)
                # write_rows already records the count; nothing else to do here.
                _ = count
            # Also the deterministic per-book assets (source doc, cover, rendered
            # page stills) so they travel even when no row references them by key.
            referenced_keys.update(deterministic_book_keys(book_id, page_count=page_count))
            if include_blobs:
                await self._blobs.export_keys(writer, sorted(referenced_keys))
            # Final manifest meta: record blob stats for inspection.
            blob_meta = {
                "blobs_exported": self._blobs.exported,
                "blobs_missing": list(self._blobs.missing),
            }
            manifest.meta.update(blob_meta)
            writer.update_meta(blob_meta)
        return manifest.sealed()

    async def _write_table(
        self,
        writer: ArchiveWriter,
        table: str,
        book_id: str,
        extractor: Any,
        referenced_keys: set[str],
    ) -> int:
        """Stream one table into the archive, accumulating referenced keys.

        ``write_rows`` wants a sync iterable, so this collects the table's rows
        into a list first. Tables are bounded per book (≤ a few thousand rows), so
        a per-table list is acceptable; the actually-large data — the blob
        payloads — is still streamed separately, never held all at once.
        """
        rows: list[dict[str, Any]] = []
        async for row in self._reader.stream_table(table, book_id):
            if extractor is not None:
                referenced_keys.update(extractor(row))
            rows.append(row)
        return writer.write_rows(table, rows)


async def export_book_to_stream(
    session: AsyncSession,
    book_id: str,
    stream: IO[bytes],
    *,
    blob_store: BlobStore,
    include_blobs: bool = True,
) -> ArchiveManifest:
    """Convenience wrapper: export ``book_id`` to ``stream``."""
    return await BookExporter(session, blob_store=blob_store).export(
        book_id, stream, include_blobs=include_blobs
    )


__all__ = ["BookExporter", "export_book_to_stream"]
