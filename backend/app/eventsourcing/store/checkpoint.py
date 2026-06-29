"""Checkpoint stores — durable positions for catch-up subscriptions.

A projection reads the global log forward and records how far it has gotten in a
:class:`~contracts.Checkpoint`. On restart it resumes from ``position + 1``;
because the global log is gap-free, "I've processed everything ≤ position" is a
sound, complete statement (no tracking-gap window needed).

Two implementations behind :class:`~contracts.CheckpointStore`:

* :class:`InMemoryCheckpointStore` — for unit tests (zero infra).
* :class:`PostgresCheckpointStore` — over ``es_checkpoints``; an upsert keeps the
  one row per subscription. It only flushes; the caller's unit of work commits —
  so a projection can advance its checkpoint *and* write its read-model rows in
  one transaction (exactly-once projection).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.eventsourcing.store.contracts import Checkpoint, CheckpointStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class InMemoryCheckpointStore:
    """An in-process :class:`~contracts.CheckpointStore`."""

    def __init__(self) -> None:
        self._checkpoints: dict[str, Checkpoint] = {}

    async def load(self, subscription: str) -> Checkpoint:
        return self._checkpoints.get(subscription, Checkpoint(subscription=subscription))

    async def save(self, checkpoint: Checkpoint) -> None:
        self._checkpoints[checkpoint.subscription] = checkpoint


class PostgresCheckpointStore:
    """A :class:`~contracts.CheckpointStore` over ``es_checkpoints``."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def load(self, subscription: str) -> Checkpoint:
        from sqlalchemy import select

        from app.eventsourcing.store.models import EventStoreCheckpoint

        row = (
            await self.session.execute(
                select(EventStoreCheckpoint).where(
                    EventStoreCheckpoint.subscription == subscription
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return Checkpoint(subscription=subscription)
        return Checkpoint(
            subscription=row.subscription,
            position=row.position,
            status=CheckpointStatus(row.status),
            events_processed=row.events_processed,
            last_error=row.last_error,
        )

    async def save(self, checkpoint: Checkpoint) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from app.eventsourcing.store.models import EventStoreCheckpoint

        stmt = (
            pg_insert(EventStoreCheckpoint)
            .values(
                subscription=checkpoint.subscription,
                position=checkpoint.position,
                status=checkpoint.status.value,
                events_processed=checkpoint.events_processed,
                last_error=checkpoint.last_error,
            )
            .on_conflict_do_update(
                index_elements=[EventStoreCheckpoint.subscription],
                set_={
                    "position": checkpoint.position,
                    "status": checkpoint.status.value,
                    "events_processed": checkpoint.events_processed,
                    "last_error": checkpoint.last_error,
                },
            )
        )
        await self.session.execute(stmt)
        await self.session.flush()


__all__ = ["InMemoryCheckpointStore", "PostgresCheckpointStore"]
