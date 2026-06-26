"""Repository for versioned canon entities (kinora.md §8.1, §8.4)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import or_, select, text, update

from app.db.base import new_id
from app.db.models.entity import Entity
from app.db.models.enums import EntityType
from app.db.repositories.base import BaseRepository


def _entity_lock_key(book_id: str, entity_key: str) -> int:
    """A stable 63-bit signed advisory-lock key for one (book, entity) chain."""
    raw = f"kinora:entity:{book_id}:{entity_key}".encode()
    return int.from_bytes(hashlib.sha1(raw).digest()[:8], "big", signed=True)


class EntityRepo(BaseRepository):
    """Create, time-travel-read, and embed versioned canon entities."""

    async def upsert_new_version(
        self,
        *,
        book_id: str,
        entity_key: str,
        entity_type: EntityType,
        name: str,
        valid_from_beat: int,
        aliases: list[str] | None = None,
        description: str | None = None,
        appearance: dict[str, Any] | None = None,
        voice: dict[str, Any] | None = None,
        style_tokens: dict[str, Any] | None = None,
        first_appearance: dict[str, Any] | None = None,
        embedding: Sequence[float] | None = None,
        entity_id: str | None = None,
    ) -> int:
        """Insert the next version of ``entity_key`` and return its version number.

        The currently-open version (the one with ``valid_to_beat IS NULL``) has
        its interval closed at ``valid_from_beat`` and is recorded as the new
        version's ``supersedes`` target. The boundary beat resolves to the newer
        version (``get_as_of_beat`` prefers the highest version on a tie).

        A transaction-scoped advisory lock on the (book, entity) chain serializes
        concurrent writers: without it two upserts could read the same open
        version, both close it, and collide on ``version`` (UniqueConstraint) or
        leave a broken interval. The lock releases on commit/rollback.
        """
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:k)").bindparams(
                k=_entity_lock_key(book_id, entity_key)
            )
        )
        existing = list(
            (
                await self.session.execute(
                    select(Entity)
                    .where(Entity.book_id == book_id, Entity.entity_key == entity_key)
                    .order_by(Entity.version.desc())
                )
            )
            .scalars()
            .all()
        )

        next_version = existing[0].version + 1 if existing else 1

        supersedes_id: str | None = None
        prior_open = next((e for e in existing if e.valid_to_beat is None), None)
        if prior_open is not None:
            prior_open.valid_to_beat = valid_from_beat
            supersedes_id = prior_open.id
        elif existing:
            supersedes_id = existing[0].id

        entity = Entity(
            id=entity_id or new_id(),
            book_id=book_id,
            entity_key=entity_key,
            type=entity_type,
            name=name,
            aliases=aliases,
            description=description,
            appearance=appearance,
            voice=voice,
            style_tokens=style_tokens,
            first_appearance=first_appearance,
            embedding=list(embedding) if embedding is not None else None,
            version=next_version,
            valid_from_beat=valid_from_beat,
            valid_to_beat=None,
            supersedes=supersedes_id,
        )
        self.session.add(entity)
        await self.session.flush()
        return next_version

    async def get_as_of_beat(
        self, book_id: str, entity_key: str, beat: int
    ) -> Entity | None:
        """Return the entity version valid *as of* ``beat`` (highest version wins)."""
        stmt = (
            select(Entity)
            .where(
                Entity.book_id == book_id,
                Entity.entity_key == entity_key,
                Entity.valid_from_beat <= beat,
                or_(Entity.valid_to_beat.is_(None), Entity.valid_to_beat >= beat),
            )
            .order_by(Entity.version.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def get_present_as_of_beat(
        self, book_id: str, entity_keys: Sequence[str], beat: int
    ) -> dict[str, Entity]:
        """Batch ``get_as_of_beat`` for many keys in one round-trip.

        Returns a ``{entity_key: active version}`` map (highest version wins per
        key). Equivalent to calling :meth:`get_as_of_beat` once per key, but a
        single query — used by the canon read path to avoid an N+1 over a beat's
        entities (§8.4).
        """
        if not entity_keys:
            return {}
        stmt = (
            select(Entity)
            .where(
                Entity.book_id == book_id,
                Entity.entity_key.in_(list(entity_keys)),
                Entity.valid_from_beat <= beat,
                or_(Entity.valid_to_beat.is_(None), Entity.valid_to_beat >= beat),
            )
            .order_by(Entity.entity_key, Entity.version.desc())
            .distinct(Entity.entity_key)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {e.entity_key: e for e in rows}

    async def list_active_at_beat(
        self, book_id: str, beat: int, kinds: Iterable[EntityType] | None = None
    ) -> list[Entity]:
        """Return the active version of every entity at ``beat`` (one row per key)."""
        stmt = (
            select(Entity)
            .where(
                Entity.book_id == book_id,
                Entity.valid_from_beat <= beat,
                or_(Entity.valid_to_beat.is_(None), Entity.valid_to_beat >= beat),
            )
            .order_by(Entity.entity_key, Entity.version.desc())
            .distinct(Entity.entity_key)
        )
        kinds_list = list(kinds) if kinds is not None else None
        if kinds_list:
            stmt = stmt.where(Entity.type.in_(kinds_list))
        return list((await self.session.execute(stmt)).scalars().all())

    async def set_embedding(self, entity_id: str, embedding: Sequence[float]) -> None:
        """Attach/replace the 1152-d appearance embedding for one entity version."""
        await self.session.execute(
            update(Entity).where(Entity.id == entity_id).values(embedding=list(embedding))
        )
        await self.session.flush()
