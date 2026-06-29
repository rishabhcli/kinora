"""Position-stamped projection snapshots — replay acceleration.

A full rebuild replays the *entire* log into a read model (always correct, but
O(all events)). A **snapshot** captures the complete materialised namespace at a
known ``global_position`` so a later rebuild can **restore the snapshot and
replay only the tail** (events after the snapshot position) — turning an O(N)
rebuild into O(events-since-snapshot). This is the standard event-sourcing
snapshotting optimisation, applied to read models rather than aggregates.

The snapshot is a plain, JSON-able capture:

* ``projection`` + ``projection_version`` — whose view, at which fold version (a
  snapshot taken under an older fold is ignored after a version bump, since the
  fold logic changed; see :mod:`app.eventsourcing.projections.versioning`).
* ``position`` — the global position the captured rows reflect.
* ``rows`` — ``{key: value}`` for every row in the namespace at that position.

:class:`SnapshotStore` is the seam; :class:`InMemorySnapshotStore` is the
deterministic fake. :class:`SnapshotPolicy` decides *when* to snapshot (every N
events) so the runtime can snapshot opportunistically during catch-up without
the projection author thinking about it.

Snapshots are an **optimisation, never a source of truth**: the log is. A
corrupt/absent snapshot only costs a longer replay, so restoring one is always
safe to skip. :func:`restore_into` writes a snapshot's rows into a target store;
the runtime then replays from ``snapshot.position`` forward.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from app.eventsourcing.projections.contracts import GlobalPosition
from app.eventsourcing.projections.readmodel import ReadModelStore


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A complete capture of one projection's read model at a global position."""

    projection: str
    position: GlobalPosition
    rows: dict[str, dict[str, Any]]
    projection_version: int = 1
    captured_at: datetime | None = None

    @property
    def row_count(self) -> int:
        return len(self.rows)


@runtime_checkable
class SnapshotStore(Protocol):
    """Persistence for the most-recent snapshot per projection."""

    async def save(self, snapshot: Snapshot) -> None:
        """Store ``snapshot`` as the latest for its projection (replacing any prior)."""
        ...

    async def latest(self, projection: str) -> Snapshot | None:
        """The most-recent snapshot for ``projection`` (or ``None``)."""
        ...

    async def delete(self, projection: str) -> bool:
        """Drop the projection's snapshot; return whether one existed."""
        ...


class InMemorySnapshotStore:
    """A deterministic in-process :class:`SnapshotStore` (one snapshot per projection)."""

    def __init__(self) -> None:
        self._snapshots: dict[str, Snapshot] = {}

    async def save(self, snapshot: Snapshot) -> None:
        # Keep the highest-position snapshot if an older one races in late.
        prior = self._snapshots.get(snapshot.projection)
        if prior is not None and prior.position > snapshot.position:
            return
        self._snapshots[snapshot.projection] = Snapshot(
            projection=snapshot.projection,
            position=snapshot.position,
            rows=copy.deepcopy(snapshot.rows),
            projection_version=snapshot.projection_version,
            captured_at=snapshot.captured_at,
        )

    async def latest(self, projection: str) -> Snapshot | None:
        snap = self._snapshots.get(projection)
        if snap is None:
            return None
        return Snapshot(
            projection=snap.projection,
            position=snap.position,
            rows=copy.deepcopy(snap.rows),
            projection_version=snap.projection_version,
            captured_at=snap.captured_at,
        )

    async def delete(self, projection: str) -> bool:
        return self._snapshots.pop(projection, None) is not None


@dataclass(slots=True)
class SnapshotPolicy:
    """Decides when to snapshot during catch-up (every ``interval`` events; 0 = off)."""

    interval: int = 0

    def should_snapshot(self, applied_since_last: int) -> bool:
        if self.interval <= 0:
            return False
        return applied_since_last >= self.interval


async def capture(
    store: ReadModelStore,
    *,
    projection: str,
    namespace: str,
    position: GlobalPosition,
    projection_version: int = 1,
) -> Snapshot:
    """Build a :class:`Snapshot` from the current rows in ``namespace``."""
    rows = await store.list(namespace)
    return Snapshot(
        projection=projection,
        position=position,
        rows={r.key: dict(r.value) for r in rows},
        projection_version=projection_version,
        captured_at=datetime.now(UTC),
    )


async def restore_into(
    store: ReadModelStore, namespace: str, snapshot: Snapshot
) -> int:
    """Write a snapshot's rows into ``namespace`` (clearing it first); return row count.

    The namespace is cleared first so restoring a snapshot of a *smaller* view
    cannot leave stale rows from a larger prior state.
    """
    await store.clear(namespace)
    for key, value in snapshot.rows.items():
        await store.put(namespace, key, value)
    return len(snapshot.rows)


__all__ = [
    "InMemorySnapshotStore",
    "Snapshot",
    "SnapshotPolicy",
    "SnapshotStore",
    "capture",
    "restore_into",
]
