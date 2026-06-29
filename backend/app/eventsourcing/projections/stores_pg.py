"""Postgres-backed :class:`ReadModelStore`, :class:`CheckpointStore`, and
:class:`SlotDirectory`.

These wrap the ``esproj_*`` ORM models (:mod:`app.eventsourcing.projections.models`)
behind the same protocols the in-memory stores satisfy, so the runtime, lag
tracker, temporal projector, and blue-green rebuilder are agnostic to whether
they are talking to the deterministic in-memory fakes (tests) or Postgres
(production). Each operation runs in its own committing unit of work via the
injected :class:`~app.composition.SessionFactory`, matching
:mod:`app.analytics.store_pg` and the rest of the composition root.

Concurrency notes:

* **Read-model upsert** uses ``INSERT ... ON CONFLICT (namespace, key) DO
  UPDATE`` with ``version = esproj_read_models.version + 1`` computed in SQL, so
  two writers racing the same key still produce a monotonically increasing
  version and never lose the row.
* **Checkpoint advance** is guarded so ``position`` only moves forward
  (``GREATEST(existing, :pos)``) — a stale/duplicate advance is a no-op even
  under a race, preserving the at-least-once invariant.
* **Applied-event dedupe** relies on the ``(projection, event_id)`` unique
  constraint: ``INSERT ... ON CONFLICT DO NOTHING`` returning the row count tells
  the runtime whether the event is newly applied (1) or a re-delivery (0).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.base import new_id
from app.eventsourcing.projections.bluegreen import Slot
from app.eventsourcing.projections.checkpoints import (
    ProjectionCheckpoint,
    ProjectionStatus,
)
from app.eventsourcing.projections.contracts import NO_POSITION, GlobalPosition
from app.eventsourcing.projections.models import (
    AppliedEventRecord,
    ProjectionCheckpointRecord,
    ReadModelRecord,
)
from app.eventsourcing.projections.readmodel import ReadModelRow

if TYPE_CHECKING:
    from app.composition import SessionFactory


class PostgresReadModelStore:
    """A :class:`ReadModelStore` backed by ``esproj_read_models``."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

    async def get(self, namespace: str, key: str) -> ReadModelRow | None:
        async with self._sf() as db:
            row = (
                await db.execute(
                    select(ReadModelRecord).where(
                        ReadModelRecord.namespace == namespace,
                        ReadModelRecord.key == key,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return ReadModelRow(key=row.key, value=dict(row.value), version=row.version)

    async def put(self, namespace: str, key: str, value: dict[str, Any]) -> ReadModelRow:
        async with self._sf() as db:
            stmt = (
                pg_insert(ReadModelRecord)
                .values(
                    id=new_id(),
                    namespace=namespace,
                    key=key,
                    value=value,
                    version=1,
                )
                .on_conflict_do_update(
                    constraint="uq_esproj_read_models_ns_key",
                    set_={
                        "value": value,
                        "version": ReadModelRecord.version + 1,
                    },
                )
                .returning(ReadModelRecord.version)
            )
            version = (await db.execute(stmt)).scalar_one()
            return ReadModelRow(key=key, value=dict(value), version=version)

    async def delete(self, namespace: str, key: str) -> bool:
        async with self._sf() as db:
            result = cast(
                CursorResult[Any],
                await db.execute(
                    delete(ReadModelRecord).where(
                        ReadModelRecord.namespace == namespace,
                        ReadModelRecord.key == key,
                    )
                ),
            )
            return bool(result.rowcount and result.rowcount > 0)

    async def list(
        self,
        namespace: str,
        *,
        prefix: str | None = None,
        limit: int | None = None,
    ) -> list[ReadModelRow]:
        async with self._sf() as db:
            stmt = (
                select(ReadModelRecord)
                .where(ReadModelRecord.namespace == namespace)
                .order_by(ReadModelRecord.key)
            )
            if prefix is not None:
                stmt = stmt.where(ReadModelRecord.key.startswith(prefix))
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = (await db.execute(stmt)).scalars().all()
            return [
                ReadModelRow(key=r.key, value=dict(r.value), version=r.version) for r in rows
            ]

    async def clear(self, namespace: str) -> int:
        async with self._sf() as db:
            result = cast(
                CursorResult[Any],
                await db.execute(
                    delete(ReadModelRecord).where(ReadModelRecord.namespace == namespace)
                ),
            )
            return int(result.rowcount or 0)

    async def count(self, namespace: str) -> int:
        async with self._sf() as db:
            return int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(ReadModelRecord)
                        .where(ReadModelRecord.namespace == namespace)
                    )
                ).scalar_one()
            )


def _record_to_checkpoint(row: ProjectionCheckpointRecord) -> ProjectionCheckpoint:
    return ProjectionCheckpoint(
        projection=row.projection,
        position=row.position,
        status=ProjectionStatus(row.status),
        observed_head=row.observed_head,
        error_count=row.error_count,
        last_error=row.last_error,
        projection_version=row.projection_version,
        updated_at=row.updated_at,
    )


class PostgresCheckpointStore:
    """A :class:`CheckpointStore` + :class:`SlotDirectory` backed by ``esproj_checkpoints``.

    Doubles as the slot directory: the active blue/green slot is stored on the
    *canonical* projection's checkpoint row (``active_slot``), so one table holds
    both position tracking and the swap pointer.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

    async def _load_record(
        self, db: Any, projection: str
    ) -> ProjectionCheckpointRecord | None:
        return (
            await db.execute(
                select(ProjectionCheckpointRecord).where(
                    ProjectionCheckpointRecord.projection == projection
                )
            )
        ).scalar_one_or_none()

    async def _ensure(self, db: Any, projection: str) -> ProjectionCheckpointRecord:
        row = await self._load_record(db, projection)
        if row is None:
            row = ProjectionCheckpointRecord(
                id=new_id(),
                projection=projection,
                position=NO_POSITION,
                observed_head=NO_POSITION,
                status=ProjectionStatus.CATCHING_UP.value,
                error_count=0,
                projection_version=1,
            )
            db.add(row)
            await db.flush()
        return row

    async def load(self, projection: str) -> ProjectionCheckpoint:
        async with self._sf() as db:
            row = await self._load_record(db, projection)
            if row is None:
                return ProjectionCheckpoint(projection=projection)
            return _record_to_checkpoint(row)

    async def advance(
        self,
        projection: str,
        position: GlobalPosition,
        *,
        status: ProjectionStatus | None = None,
        observed_head: GlobalPosition | None = None,
    ) -> ProjectionCheckpoint:
        async with self._sf() as db:
            row = await self._ensure(db, projection)
            row.position = max(row.position, position)
            if observed_head is not None:
                row.observed_head = max(row.observed_head, observed_head)
            else:
                row.observed_head = max(row.observed_head, row.position)
            if status is not None:
                row.status = status.value
            await db.flush()
            return _record_to_checkpoint(row)

    async def record_error(self, projection: str, error: str) -> ProjectionCheckpoint:
        async with self._sf() as db:
            row = await self._ensure(db, projection)
            row.error_count += 1
            row.last_error = error
            row.status = ProjectionStatus.FAULTED.value
            await db.flush()
            return _record_to_checkpoint(row)

    async def set_status(
        self, projection: str, status: ProjectionStatus
    ) -> ProjectionCheckpoint:
        async with self._sf() as db:
            row = await self._ensure(db, projection)
            row.status = status.value
            await db.flush()
            return _record_to_checkpoint(row)

    async def set_projection_version(
        self, projection: str, version: int
    ) -> ProjectionCheckpoint:
        async with self._sf() as db:
            row = await self._ensure(db, projection)
            row.projection_version = version
            await db.flush()
            return _record_to_checkpoint(row)

    async def reset(self, projection: str) -> ProjectionCheckpoint:
        async with self._sf() as db:
            row = await self._ensure(db, projection)
            row.position = NO_POSITION
            row.observed_head = NO_POSITION
            row.status = ProjectionStatus.CATCHING_UP.value
            row.error_count = 0
            row.last_error = None
            await db.flush()
            # Drop this projection's applied-event ledger so a rebuild re-folds all.
            await db.execute(
                delete(AppliedEventRecord).where(
                    AppliedEventRecord.projection == projection
                )
            )
            return _record_to_checkpoint(row)

    async def mark_applied(self, projection: str, event_id: str) -> bool:
        async with self._sf() as db:
            stmt = (
                pg_insert(AppliedEventRecord)
                .values(
                    id=new_id(),
                    projection=projection,
                    event_id=event_id,
                    position=NO_POSITION,
                )
                .on_conflict_do_nothing(constraint="uq_esproj_applied_proj_event")
                .returning(AppliedEventRecord.id)
            )
            inserted = (await db.execute(stmt)).scalar_one_or_none()
            return inserted is not None

    async def was_applied(self, projection: str, event_id: str) -> bool:
        async with self._sf() as db:
            found = (
                await db.execute(
                    select(AppliedEventRecord.id).where(
                        AppliedEventRecord.projection == projection,
                        AppliedEventRecord.event_id == event_id,
                    )
                )
            ).scalar_one_or_none()
            return found is not None

    # -- SlotDirectory ------------------------------------------------------- #

    async def has_active(self, projection: str) -> bool:
        async with self._sf() as db:
            row = await self._load_record(db, projection)
            return row is not None and row.active_slot is not None

    async def active(self, projection: str) -> Slot:
        async with self._sf() as db:
            row = await self._load_record(db, projection)
            if row is None or row.active_slot is None:
                return Slot.BLUE
            return Slot(row.active_slot)

    async def set_active(self, projection: str, slot: Slot) -> None:
        async with self._sf() as db:
            row = await self._ensure(db, projection)
            row.active_slot = slot.value
            await db.flush()


__all__ = [
    "PostgresCheckpointStore",
    "PostgresReadModelStore",
]
