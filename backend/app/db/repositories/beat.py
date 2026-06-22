"""Repository for beats — the planning atom and as-of-beat bridge (kinora.md §4.2)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select

from app.db.base import new_id
from app.db.models.beat import Beat
from app.db.repositories.base import BaseRepository


class BeatRepo(BaseRepository):
    """Create and query beats; expose the entity_keys present in a beat."""

    async def create(
        self,
        *,
        book_id: str,
        scene_id: str,
        beat_index: int,
        summary: str,
        entities: list[str] | None = None,
        described_visuals: str | None = None,
        mood: str | None = None,
        source_span: dict[str, Any] | None = None,
        beat_id: str | None = None,
    ) -> Beat:
        """Insert one beat (``beat_id`` may be a semantic id like ``beat_0034``)."""
        beat = Beat(
            id=beat_id or new_id(),
            book_id=book_id,
            scene_id=scene_id,
            beat_index=beat_index,
            summary=summary,
            entities=list(entities) if entities is not None else [],
            described_visuals=described_visuals,
            mood=mood,
            source_span=source_span,
        )
        self.session.add(beat)
        await self.session.flush()
        return beat

    async def create_many(self, beats: Iterable[dict[str, Any]]) -> int:
        """Bulk-insert beats (each dict mirrors :meth:`create`'s columns)."""
        rows: list[Beat] = []
        for raw in beats:
            data = dict(raw)
            data.setdefault("id", new_id())
            data.setdefault("entities", [])
            rows.append(Beat(**data))
        self.session.add_all(rows)
        await self.session.flush()
        return len(rows)

    async def get(self, beat_id: str) -> Beat | None:
        """Fetch a beat by id."""
        return await self.session.get(Beat, beat_id)

    async def list_by_scene(self, scene_id: str) -> list[Beat]:
        """Return a scene's beats in reading order."""
        stmt = select(Beat).where(Beat.scene_id == scene_id).order_by(Beat.beat_index)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_entities(self, beat_id: str) -> list[str]:
        """Return the canon ``entity_key`` s present in a beat (empty if none/unknown)."""
        beat = await self.session.get(Beat, beat_id)
        if beat is None or beat.entities is None:
            return []
        return list(beat.entities)
