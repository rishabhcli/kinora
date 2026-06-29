"""The snapshot-store seam (facet A) + an in-memory fallback.

Replaying a long stream from event 1 on every load is wasteful once a shot or
session has accumulated many events. A **snapshot** captures an aggregate's state
at a known version so the repository can load the snapshot and replay only the
*tail* (events after it).

Facet A owns the production snapshot persistence; facet B consumes the
:class:`SnapshotStore` protocol and supplies an in-memory fallback so the
snapshotting load path is exercisable in tests. A snapshot is an opaque ``state``
mapping plus the ``version`` it reflects — the aggregate decides how to
encode/decode it (see :class:`~app.eventsourcing.domain.snapshotting.Snapshotter`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A point-in-time encoding of an aggregate's state.

    Attributes:
        stream_id: the stream this snapshot summarises.
        version: the stream version the ``state`` reflects (replay events > this).
        state: the aggregate's encoded state (a JSON-ready mapping).
    """

    stream_id: str
    version: int
    state: Mapping[str, object]


@runtime_checkable
class SnapshotStore(Protocol):
    """Stores and retrieves the latest :class:`Snapshot` per stream."""

    async def save(self, snapshot: Snapshot) -> None:
        """Persist ``snapshot`` as the latest snapshot for its stream."""
        ...

    async def load(self, stream_id: str) -> Snapshot | None:
        """Return the latest snapshot for ``stream_id`` (``None`` if none)."""
        ...


@dataclass(slots=True)
class InMemorySnapshotStore:
    """A dict-backed :class:`SnapshotStore` (the reference fallback)."""

    snapshots: dict[str, Snapshot] = field(default_factory=dict)

    async def save(self, snapshot: Snapshot) -> None:
        existing = self.snapshots.get(snapshot.stream_id)
        # Keep the highest-version snapshot (a late, older save never regresses).
        if existing is None or snapshot.version >= existing.version:
            self.snapshots[snapshot.stream_id] = snapshot

    async def load(self, stream_id: str) -> Snapshot | None:
        return self.snapshots.get(stream_id)


__all__ = ["InMemorySnapshotStore", "Snapshot", "SnapshotStore"]
