"""DB-backed persistence for the ML-data platform (the durable, immutable mirror).

The pure in-memory components (:mod:`app.mlplatform.datasets.versioning`) are the
source of truth at runtime; this module mirrors committed versions to the three
``mldata_*`` tables (:mod:`app.db.models.mldata`) so a training set survives a
restart and can be served without replaying the pipeline.

It follows the repo's repository convention: it **flushes** but never
**commits** — the unit-of-work boundary owns the transaction (see
``app.db.repositories.base``). Every method is async and infra-bound; the unit
suite skips these cleanly when no test DB is configured (the pure logic is fully
covered without them).

A committed version is immutable, so :meth:`DatasetVersionStore.persist_version`
is an *insert-if-absent*: re-persisting the same content-addressed id is a no-op
(idempotent), and the examples + lineage edges are written once alongside it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import new_id
from app.db.models.mldata import (
    MLDataDatasetVersion,
    MLDataExample,
    MLDataLineageEdge,
)
from app.mlplatform.datasets.versioning import DatasetVersion


class DatasetVersionStore:
    """Persist + load immutable dataset versions over the DB."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def persist_version(self, version: DatasetVersion) -> bool:
        """Insert a version + its examples + lineage edges (idempotent no-op if present).

        Returns ``True`` when a new row was written, ``False`` when the
        content-addressed id already existed (re-persist of a frozen version).
        """
        existing = await self.session.get(MLDataDatasetVersion, version.version_id)
        if existing is not None:
            return False

        self.session.add(
            MLDataDatasetVersion(
                id=version.version_id,
                name=version.name,
                content_hash=version.content_hash,
                operation=version.operation.value,
                n_examples=version.n,
                stats=version.stats.to_dict(),
                op_params=dict(version.op_params) or None,
                tags=list(version.tags) or None,
                note=version.note or None,
                created_at=version.created_at,
            )
        )
        for ex in version.dataset.examples:
            rec = ex.to_record()
            self.session.add(
                MLDataExample(
                    id=new_id(),
                    version_id=version.version_id,
                    example_id=ex.id,
                    role=ex.role.value,
                    task=ex.task.value,
                    split=ex.split.value,
                    content_hash=ex.content_hash,
                    reward=ex.reward,
                    scrubbed=ex.scrubbed,
                    record=rec,
                    book_id=ex.book_id,
                    session_id=ex.session_id,
                    created_at=ex.created_at,
                )
            )
        for parent in version.parents:
            self.session.add(
                MLDataLineageEdge(
                    id=new_id(), parent_id=parent, version_id=version.version_id
                )
            )
        await self.session.flush()
        return True

    async def get_version_row(self, version_id: str) -> MLDataDatasetVersion | None:
        return await self.session.get(MLDataDatasetVersion, version_id)

    async def latest_version_id(self, name: str) -> str | None:
        """The newest committed version id for a named dataset."""
        stmt = (
            select(MLDataDatasetVersion.id)
            .where(MLDataDatasetVersion.name == name)
            .order_by(MLDataDatasetVersion.created_at.desc())
            .limit(1)
        )
        return await self.session.scalar(stmt)

    async def history_ids(self, name: str) -> list[str]:
        """All version ids for a name, oldest → newest."""
        stmt = (
            select(MLDataDatasetVersion.id)
            .where(MLDataDatasetVersion.name == name)
            .order_by(MLDataDatasetVersion.created_at.asc())
        )
        return list((await self.session.scalars(stmt)).all())

    async def names(self) -> list[str]:
        stmt = select(MLDataDatasetVersion.name).distinct()
        return sorted((await self.session.scalars(stmt)).all())

    async def example_records(
        self, version_id: str, *, split: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """The frozen example records of a version (optionally one split)."""
        stmt = select(MLDataExample.record).where(MLDataExample.version_id == version_id)
        if split is not None:
            stmt = stmt.where(MLDataExample.split == split)
        stmt = stmt.order_by(MLDataExample.example_id)
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await self.session.scalars(stmt)).all()
        return [dict(r) for r in rows]

    async def lineage_parents(self, version_id: str) -> list[str]:
        """The immediate parents of a version (one DAG hop up)."""
        stmt = select(MLDataLineageEdge.parent_id).where(
            MLDataLineageEdge.version_id == version_id
        )
        return list((await self.session.scalars(stmt)).all())

    async def lineage_ancestry(self, version_id: str) -> list[str]:
        """The full transitive ancestry of a version (recursive DAG walk)."""
        seen: set[str] = set()
        order: list[str] = []
        frontier = [version_id]
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            order.append(current)
            frontier.extend(await self.lineage_parents(current))
        return order


__all__ = ["DatasetVersionStore"]
