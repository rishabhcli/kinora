"""The indexing pipeline — project canon/library rows → documents → the index.

Two entry points:

* :meth:`IndexingPipeline.index_book` — incremental: (re)index one book's
  documents into the *live* index version. Called after ingest finishes / a
  canon edit / a shot is accepted, so search stays fresh without a full rebuild.
* :meth:`IndexingPipeline.reindex_all` — bulk: build the entire corpus into a
  *fresh* index version, then atomically swap the alias to it (zero-downtime).

Embeddings for the dense arm: canon entities and shots already carry a 1152-d
vector (§8.1 / §8.2), so those documents are projected with it. The text-only
documents (book / page / scene / beat) are embedded here through the
:class:`~app.memory.interfaces.Embedder` protocol — the same provider the memory
layer uses — so a search injects a *fake* embedder in tests and never hits the
network (zero credits). Embedding is best-effort: a failure degrades the
document to lexical-only rather than failing the whole index pass.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models.beat import Beat
from app.db.models.book import Book, Page
from app.db.models.entity import Entity
from app.db.models.enums import ShotStatus
from app.db.models.scene import Scene
from app.db.models.shot import Shot
from app.memory.interfaces import Embedder
from app.search.alias import AliasRegistry, new_version
from app.search.documents import DocKind, SearchDocument
from app.search.index import SearchIndex
from app.search.projection import (
    project_beat,
    project_book,
    project_entity,
    project_page,
    project_scene,
    project_shot,
)

logger = get_logger("app.search.pipeline")

SessionFactory = Any

#: Kinds that arrive without a vector and are embedded from their text here.
_TEXT_EMBED_KINDS = frozenset({DocKind.BOOK, DocKind.PAGE, DocKind.SCENE, DocKind.BEAT})


@dataclass(frozen=True)
class IndexStats:
    """How a (re)index pass went: per-kind document counts + total."""

    total: int
    by_kind: dict[str, int]
    index_version: str


class IndexingPipeline:
    """Reads canon/library rows, projects them, embeds, and upserts to an index."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        embedder: Embedder | None = None,
        embed_batch: int = 8,
    ) -> None:
        self._session_factory = session_factory
        self._embedder = embedder
        self._embed_batch = embed_batch

    # -- incremental (one book) --------------------------------------------- #

    async def index_book(self, book_id: str, index: SearchIndex) -> IndexStats:
        """(Re)index every document for one book into ``index`` (incremental upsert).

        Removes the book's stale documents first so a deleted page/shot doesn't
        linger, then upserts the freshly projected set.
        """
        docs = await self._collect_book_docs(book_id)
        await self._embed_text_docs(docs)
        await index.delete_by_book(book_id)
        await index.upsert(docs)
        return self._stats(docs, getattr(index, "index_version", "live"))

    async def index_documents(
        self, docs: Sequence[SearchDocument], index: SearchIndex
    ) -> IndexStats:
        """Embed (text docs) + upsert a caller-provided document set (targeted)."""
        await self._embed_text_docs(list(docs))
        await index.upsert(docs)
        return self._stats(docs, getattr(index, "index_version", "live"))

    # -- bulk (whole corpus, versioned-alias swap) -------------------------- #

    async def reindex_all(
        self,
        *,
        make_index: Any,
        alias_registry: AliasRegistry,
        alias: str = "kinora_current",
    ) -> IndexStats:
        """Rebuild the whole corpus into a fresh version, then swap the alias.

        ``make_index(version) -> SearchIndex`` builds a backend bound to a new
        index version; for Postgres this is ``PostgresIndex.for_version``. Live
        reads keep resolving the alias to the old version until the swap.
        """
        version = new_version()
        index = make_index(version)
        ensure = getattr(index, "ensure_schema", None)
        if ensure is not None:
            await ensure()
        await index.clear()

        book_ids = await self._all_book_ids()
        total_docs: list[SearchDocument] = []
        by_kind: dict[str, int] = {}
        for book_id in book_ids:
            docs = await self._collect_book_docs(book_id)
            await self._embed_text_docs(docs)
            await index.upsert(docs)
            for d in docs:
                by_kind[d.kind.value] = by_kind.get(d.kind.value, 0) + 1
            total_docs.extend(docs)

        await alias_registry.set_alias(alias, version)
        logger.info(
            "search.reindex.complete",
            version=version,
            total=len(total_docs),
            books=len(book_ids),
        )
        return IndexStats(total=len(total_docs), by_kind=by_kind, index_version=version)

    # -- projection (DB reads) ---------------------------------------------- #

    async def _collect_book_docs(self, book_id: str) -> list[SearchDocument]:
        docs: list[SearchDocument] = []
        async with self._session_factory() as session:
            book = await session.get(Book, book_id)
            if book is not None:
                docs.append(project_book(book))
            pages = (
                (await session.execute(select(Page).where(Page.book_id == book_id)))
                .scalars()
                .all()
            )
            docs.extend(project_page(p) for p in pages)
            scenes = (
                (await session.execute(select(Scene).where(Scene.book_id == book_id)))
                .scalars()
                .all()
            )
            docs.extend(project_scene(s) for s in scenes)
            beats = (
                (await session.execute(select(Beat).where(Beat.book_id == book_id)))
                .scalars()
                .all()
            )
            docs.extend(project_beat(b) for b in beats)
            # Active canon entity versions only (§8.5 forgetting: retired drop out).
            entities = (
                (
                    await session.execute(
                        select(Entity).where(
                            Entity.book_id == book_id, Entity.valid_to_beat.is_(None)
                        )
                    )
                )
                .scalars()
                .all()
            )
            for e in entities:
                doc = project_entity(e)
                if doc is not None:
                    docs.append(doc)
            # Accepted shots only (the §8.2 episodic record worth searching).
            shots = (
                (
                    await session.execute(
                        select(Shot).where(
                            Shot.book_id == book_id, Shot.status == ShotStatus.ACCEPTED
                        )
                    )
                )
                .scalars()
                .all()
            )
            docs.extend(project_shot(s) for s in shots)
        return docs

    async def _all_book_ids(self) -> list[str]:
        async with self._session_factory() as session:
            rows = (await session.execute(select(Book.id))).scalars().all()
            return list(rows)

    # -- embeddings (text docs) --------------------------------------------- #

    async def _embed_text_docs(self, docs: list[SearchDocument]) -> None:
        """Embed the text-only documents in place (best-effort, batched)."""
        if self._embedder is None:
            return
        pending = [
            d
            for d in docs
            if d.embedding is None and d.kind in _TEXT_EMBED_KINDS and d.all_text().strip()
        ]
        if not pending:
            return
        for start in range(0, len(pending), self._embed_batch):
            batch = pending[start : start + self._embed_batch]
            texts = [d.all_text() for d in batch]
            try:
                vectors = await self._embedder.embed_texts(texts)
            except Exception as exc:  # noqa: BLE001 - degrade to lexical-only
                logger.warning("search.embed_failed", error=str(exc), count=len(batch))
                return
            for doc, vec in zip(batch, vectors, strict=False):
                doc.embedding = vec

    @staticmethod
    def _stats(docs: Sequence[SearchDocument], version: str) -> IndexStats:
        by_kind: dict[str, int] = {}
        for d in docs:
            by_kind[d.kind.value] = by_kind.get(d.kind.value, 0) + 1
        return IndexStats(total=len(docs), by_kind=by_kind, index_version=version)


__all__ = ["IndexStats", "IndexingPipeline"]
