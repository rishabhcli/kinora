"""Snapshotting — the policy + a thin alias for the Postgres snapshot store.

A snapshot is a point-in-time materialisation of an aggregate's state so that
rehydration replays only the events *after* the snapshot, turning O(stream
length) reloads into O(events-since-snapshot). The :class:`PostgresEventStore`
already implements the :class:`~contracts.SnapshotStore` protocol (``save`` /
``load_latest``); :class:`PostgresSnapshotStore` is a thin standalone wrapper for
callers that want a snapshot-only handle without an event-store instance.

:class:`SnapshotStrategy` is the pure policy the *domain* facet consults after an
append to decide whether to write a fresh snapshot — typically "every N events".
Keeping it pure and separate means the cadence is testable and tunable without
touching storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.eventsourcing.store.contracts import Snapshot
from app.eventsourcing.store.versioning import NO_EVENTS

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class SnapshotStrategy:
    """Decide when to take a snapshot (pure policy).

    ``every`` is the number of events between snapshots. With ``every=20`` a
    snapshot is taken when crossing version 19→20, 39→40, … i.e. whenever the
    new version is a positive multiple of ``every`` or the interval since the
    last snapshot reached ``every``.
    """

    every: int = 50

    def __post_init__(self) -> None:
        if self.every <= 0:
            raise ValueError("snapshot interval 'every' must be >= 1")

    def should_snapshot(self, *, new_version: int, last_snapshot_version: int = NO_EVENTS) -> bool:
        """Whether to snapshot at ``new_version`` given the last snapshot's version."""
        if new_version < 0:
            return False
        since = new_version - last_snapshot_version
        # Snapshot when we've accumulated >= `every` events since the last one,
        # or when the version lands exactly on a multiple of `every`.
        return since >= self.every or ((new_version + 1) % self.every == 0)


class PostgresSnapshotStore:
    """A standalone :class:`~contracts.SnapshotStore` over ``es_snapshots``.

    Delegates to :class:`PostgresEventStore`'s snapshot methods so there is one
    implementation of the upsert/monotone semantics.
    """

    def __init__(self, session: AsyncSession) -> None:
        from app.eventsourcing.store.postgres import PostgresEventStore

        self._store = PostgresEventStore(session)

    async def save(self, snapshot: Snapshot) -> None:
        await self._store.save(snapshot)

    async def load_latest(
        self, stream_id: str, *, snapshot_type: str = "default"
    ) -> Snapshot | None:
        return await self._store.load_latest(stream_id, snapshot_type=snapshot_type)


__all__ = ["PostgresSnapshotStore", "SnapshotStrategy"]
