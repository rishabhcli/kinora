"""Canon-graph export — the §8 bible as a portable, checksummed archive.

A canon archive is a ``kind=canon_graph`` ``.kinora`` containing only the canon
tables — versioned entities, continuity states, the bitemporal engine
(bitemporal_states + canon_branches + canon_audit) — plus the reference-asset
blobs the entities point at (locked keyframes, voice refs). It is what you ship
to move a story bible between books/deployments, or to seed a new adaptation from
an existing canon without dragging along its shots and films.

Unlike a full book bundle, a canon archive carries **no ``books`` row** (the
target book is named at import time), so canon export embeds the source book id +
title in the manifest meta for provenance only.
"""

from __future__ import annotations

from typing import IO, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.dataportability.blobs import BlobExporter
from app.dataportability.codec import ArchiveWriter
from app.dataportability.dbio import BookReader
from app.dataportability.keys import keys_from_entity
from app.dataportability.manifest import ArchiveKind, ArchiveManifest
from app.dataportability.serialization import CANON_TABLES
from app.memory.interfaces import BlobStore


class CanonExporter:
    """Export a book's canon graph (only) to a ``.kinora`` archive."""

    def __init__(self, session: AsyncSession, *, blob_store: BlobStore) -> None:
        self._reader = BookReader(session)
        self._blobs = BlobExporter(blob_store)
        self._session = session

    async def export(
        self,
        book_id: str,
        stream: IO[bytes],
        *,
        include_blobs: bool = True,
    ) -> ArchiveManifest:
        """Write the canon-only archive to ``stream``; return the sealed manifest."""
        from app.db.models.book import Book

        book = await self._session.get(Book, book_id)
        title = getattr(book, "title", None) if book is not None else None

        manifest = ArchiveManifest(
            kind=ArchiveKind.CANON,
            meta={"source_book_id": book_id, "title": title},
        )
        referenced_keys: set[str] = set()

        with ArchiveWriter(stream, manifest) as writer:
            for table in CANON_TABLES:
                rows: list[dict[str, Any]] = []
                async for row in self._reader.stream_table(table, book_id):
                    if table == "entities":
                        referenced_keys.update(keys_from_entity(row))
                    rows.append(row)
                writer.write_rows(table, rows)
            if include_blobs:
                await self._blobs.export_keys(writer, sorted(referenced_keys))
            blob_meta = {
                "blobs_exported": self._blobs.exported,
                "blobs_missing": list(self._blobs.missing),
            }
            manifest.meta.update(blob_meta)
            writer.update_meta(blob_meta)
        return manifest.sealed()


async def export_canon_to_stream(
    session: AsyncSession,
    book_id: str,
    stream: IO[bytes],
    *,
    blob_store: BlobStore,
    include_blobs: bool = True,
) -> ArchiveManifest:
    """Convenience wrapper: export ``book_id``'s canon to ``stream``."""
    return await CanonExporter(session, blob_store=blob_store).export(
        book_id, stream, include_blobs=include_blobs
    )


__all__ = ["CanonExporter", "export_canon_to_stream"]
