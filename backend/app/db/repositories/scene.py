"""Repository for scenes — the stitch-boundary unit (kinora.md §4.2, §8.4)."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, delete, select

from app.db.base import new_id
from app.db.models.scene import Scene
from app.db.repositories.base import BaseRepository


class SceneRepo(BaseRepository):
    """Create and query scenes; resolve the style node governing a scene."""

    async def delete_for_book(self, book_id: str) -> int:
        """Delete a book's scenes (cascading its beats); returns rows removed.

        The §9.1 shot-plan step is re-runnable: a re-ingest (e.g. resuming a book
        that previously failed mid-pipeline, after a DashScope throttle) clears the
        prior plan first so re-inserting the same book-scoped ids does not violate
        ``pk_scenes``. ``beats.scene_id`` is ``ON DELETE CASCADE``, so deleting a
        book's scenes drops its beats too; shots are cleared by ``ShotRepo``.
        """
        result = cast(
            "CursorResult[Any]",
            await self.session.execute(delete(Scene).where(Scene.book_id == book_id)),
        )
        await self.session.flush()
        return int(result.rowcount or 0)

    async def create(
        self,
        *,
        book_id: str,
        scene_index: int,
        page_start: int,
        page_end: int,
        title: str | None = None,
        style_entity_key: str | None = None,
        scene_id: str | None = None,
    ) -> Scene:
        """Insert one scene (``scene_id`` may be a semantic id like ``scene_005``)."""
        scene = Scene(
            id=scene_id or new_id(),
            book_id=book_id,
            scene_index=scene_index,
            page_start=page_start,
            page_end=page_end,
            title=title,
            style_entity_key=style_entity_key,
        )
        self.session.add(scene)
        await self.session.flush()
        return scene

    async def get(self, scene_id: str) -> Scene | None:
        """Fetch a scene by id."""
        return await self.session.get(Scene, scene_id)

    async def list_by_book(self, book_id: str) -> list[Scene]:
        """Return a book's scenes in narrative order."""
        stmt = select(Scene).where(Scene.book_id == book_id).order_by(Scene.scene_index)
        return list((await self.session.execute(stmt)).scalars().all())

    async def style_for_scene(self, scene_id: str) -> str | None:
        """Return the Style ``entity_key`` governing ``scene_id`` (``None`` => book default).

        A scene may pin its own style node; when it does not, the caller (the
        canon retrieval policy) falls back to the book's default style.
        """
        scene = await self.session.get(Scene, scene_id)
        return scene.style_entity_key if scene is not None else None
