"""Repositories for the shot aggregate: episodic store, source-span index, cache."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models.enums import ShotStatus
from app.db.models.shot import Shot, ShotCache, SourceSpanIndex
from app.db.repositories.base import BaseRepository

# Statuses that still need scheduler/render work (i.e. not yet a committed,
# QA-passed, cached clip).
_UNCOMMITTED = tuple(s for s in ShotStatus if s is not ShotStatus.ACCEPTED)


class ShotRepo(BaseRepository):
    """CRUD plus pgvector episodic search over accepted shots (kinora.md §8.2)."""

    async def create(self, **fields: Any) -> Shot:
        """Insert a new shot row from the given column values."""
        shot = Shot(**fields)
        self.session.add(shot)
        await self.session.flush()
        return shot

    async def get(self, shot_id: str) -> Shot | None:
        """Fetch a shot by id."""
        return await self.session.get(Shot, shot_id)

    async def update(self, shot_id: str, **fields: Any) -> Shot | None:
        """Patch the given columns on a shot; returns the row (or ``None``)."""
        shot = await self.session.get(Shot, shot_id)
        if shot is None:
            return None
        for key, value in fields.items():
            setattr(shot, key, value)
        await self.session.flush()
        return shot

    async def set_status(self, shot_id: str, status: ShotStatus) -> None:
        """Transition a shot to ``status``."""
        await self.session.execute(
            update(Shot).where(Shot.id == shot_id).values(status=status)
        )
        await self.session.flush()

    async def mark_accepted(
        self, shot_id: str, *, accepted_at: datetime | None = None
    ) -> None:
        """Mark a shot accepted and stamp ``accepted_at`` (defaults to now, UTC)."""
        await self.session.execute(
            update(Shot)
            .where(Shot.id == shot_id)
            .values(status=ShotStatus.ACCEPTED, accepted_at=accepted_at or datetime.now(UTC))
        )
        await self.session.flush()

    async def episodic_search(
        self,
        book_id: str,
        embedding: Sequence[float],
        k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[Shot]:
        """Return the ``k`` nearest *accepted* shots by cosine distance (``<=>``).

        Used by ``episodic.search`` (kinora.md §8.3): "what worked before" for a
        similar beat. Optional equality ``filters`` may scope by ``scene_id``,
        ``beat_id``, or ``render_mode``.
        """
        stmt = select(Shot).where(
            Shot.book_id == book_id,
            Shot.status == ShotStatus.ACCEPTED,
            Shot.embedding.is_not(None),
        )
        if filters:
            for field in ("scene_id", "beat_id", "render_mode"):
                if field in filters:
                    stmt = stmt.where(getattr(Shot, field) == filters[field])
        # pgvector registers ``cosine_distance`` on the Vector comparator at
        # runtime; cast to Any so the typed InstrumentedAttribute doesn't trip mypy.
        distance = cast(Any, Shot.embedding).cosine_distance(list(embedding))
        stmt = stmt.order_by(distance).limit(k)
        return list((await self.session.execute(stmt)).scalars().all())


class SourceSpanRepo(BaseRepository):
    """The §4.2 sorted ``word_index → shot`` index (O(log n) scroll resolution)."""

    async def bulk_insert(self, spans: Iterable[dict[str, Any]]) -> int:
        """Bulk-insert source-span rows; returns the number inserted."""
        rows = [SourceSpanIndex(**span) for span in spans]
        self.session.add_all(rows)
        await self.session.flush()
        return len(rows)

    async def resolve_word_to_shot(self, book_id: str, word_index: int) -> Shot | None:
        """Resolve a focus word to its shot via the greatest start <= ``word_index``.

        This is the index seek the btree on ``(book_id, word_index_start)`` is
        built for: the span the reader is currently inside.
        """
        stmt = (
            select(Shot)
            .join(SourceSpanIndex, SourceSpanIndex.shot_id == Shot.id)
            .where(
                SourceSpanIndex.book_id == book_id,
                SourceSpanIndex.word_index_start <= word_index,
            )
            .order_by(SourceSpanIndex.word_index_start.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def next_uncommitted_shot(self, book_id: str, after_word: int) -> Shot | None:
        """Return the next not-yet-accepted shot after ``after_word`` (for the buffer fill)."""
        stmt = (
            select(Shot)
            .join(SourceSpanIndex, SourceSpanIndex.shot_id == Shot.id)
            .where(
                SourceSpanIndex.book_id == book_id,
                SourceSpanIndex.word_index_start > after_word,
                Shot.status.in_(_UNCOMMITTED),
            )
            .order_by(SourceSpanIndex.word_index_start.asc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()


class ShotCacheRepo(BaseRepository):
    """Content-hash keyed shot cache (kinora.md §8.7)."""

    async def get(self, shot_hash: str) -> ShotCache | None:
        """Look up a cached shot by content hash."""
        return await self.session.get(ShotCache, shot_hash)

    async def put(
        self,
        *,
        shot_hash: str,
        book_id: str,
        clip_key: str | None = None,
        last_frame_key: str | None = None,
        sync_segment: dict[str, Any] | None = None,
        qa: dict[str, Any] | None = None,
        video_seconds: float | None = None,
    ) -> ShotCache:
        """Atomically upsert a cache record (insert-or-update on ``shot_hash``)."""
        mutable = {
            "clip_key": clip_key,
            "last_frame_key": last_frame_key,
            "sync_segment": sync_segment,
            "qa": qa,
            "video_seconds": video_seconds,
        }
        stmt = (
            pg_insert(ShotCache)
            .values(shot_hash=shot_hash, book_id=book_id, **mutable)
            .on_conflict_do_update(index_elements=[ShotCache.shot_hash], set_=mutable)
        )
        await self.session.execute(stmt)
        await self.session.flush()
        record = await self.session.get(ShotCache, shot_hash, populate_existing=True)
        if record is None:  # pragma: no cover - row was just upserted
            raise RuntimeError(f"shot_cache row missing after upsert: {shot_hash}")
        return record
