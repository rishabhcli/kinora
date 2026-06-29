"""Read-model projections — the CQRS read side (facet C).

Folds the append-only event log (owned by facet A, consumed via the
:class:`EventStore` protocol in :mod:`.contracts`) into queryable read models.
See ``DESIGN.md`` for the architecture, the consumed contract, and the roadmap.

Public surface (everything a composition root / API needs):

* Contract — :class:`StoredEvent`, :class:`EventStore`, :data:`NO_POSITION`.
* Projections — :class:`Projection`, :func:`handles`.
* Stores — :class:`ReadModelStore` (+ in-memory), :class:`CheckpointStore`
  (+ in-memory), :class:`InMemoryEventStore`.
* Runtime — :class:`ProjectionRuntime`, :class:`RuntimeConfig`,
  :class:`CatchUpResult`, :class:`ProjectionFaultedError`.
* Eventual consistency — :class:`LagTracker`, :class:`ConsistencyToken`.
* Temporal (§8.5) — :class:`AsOfProjector`, :func:`diff_rows`.
* Blue-green — :class:`BlueGreenRebuilder`, :class:`Slot`.
* Composition — :class:`ProjectionRegistry`, :class:`ProjectionSupervisor`.
"""

from __future__ import annotations

from app.eventsourcing.projections.bluegreen import (
    BlueGreenRebuilder,
    InMemorySlotDirectory,
    RebuildReport,
    Slot,
    SlotDirectory,
    slot_namespace,
)
from app.eventsourcing.projections.checkpoints import (
    CheckpointStore,
    InMemoryCheckpointStore,
    ProjectionCheckpoint,
    ProjectionStatus,
)
from app.eventsourcing.projections.contracts import (
    NO_POSITION,
    EventStore,
    GlobalPosition,
    StoredEvent,
)
from app.eventsourcing.projections.lag import (
    ConsistencyToken,
    LagSnapshot,
    LagTracker,
    worst_lag,
)
from app.eventsourcing.projections.memory_eventstore import InMemoryEventStore
from app.eventsourcing.projections.projection import Projection, handles
from app.eventsourcing.projections.reader import ProjectionReader, ReadResult
from app.eventsourcing.projections.readmodel import (
    InMemoryReadModelStore,
    ReadModelRow,
    ReadModelStore,
)
from app.eventsourcing.projections.registry import (
    ProjectionRegistry,
    ProjectionSupervisor,
    default_projections,
)
from app.eventsourcing.projections.runtime import (
    CatchUpResult,
    DeadLetterSink,
    ProjectionFaultedError,
    ProjectionRuntime,
    RuntimeConfig,
)
from app.eventsourcing.projections.snapshots import (
    InMemorySnapshotStore,
    Snapshot,
    SnapshotPolicy,
    SnapshotStore,
)
from app.eventsourcing.projections.temporal import (
    AsOfProjector,
    AsOfResult,
    ViewDiff,
    diff_rows,
)
from app.eventsourcing.projections.versioning import (
    VersionAction,
    VersionDecision,
    VersionGuard,
    check_version,
)

__all__ = [
    "NO_POSITION",
    "AsOfProjector",
    "AsOfResult",
    "BlueGreenRebuilder",
    "CatchUpResult",
    "CheckpointStore",
    "ConsistencyToken",
    "DeadLetterSink",
    "EventStore",
    "GlobalPosition",
    "InMemoryCheckpointStore",
    "InMemoryEventStore",
    "InMemoryReadModelStore",
    "InMemorySlotDirectory",
    "InMemorySnapshotStore",
    "LagSnapshot",
    "LagTracker",
    "Projection",
    "ProjectionCheckpoint",
    "ProjectionFaultedError",
    "ProjectionReader",
    "ProjectionRegistry",
    "ProjectionRuntime",
    "ProjectionStatus",
    "ProjectionSupervisor",
    "ReadModelRow",
    "ReadModelStore",
    "ReadResult",
    "RebuildReport",
    "RuntimeConfig",
    "Slot",
    "SlotDirectory",
    "Snapshot",
    "SnapshotPolicy",
    "SnapshotStore",
    "StoredEvent",
    "VersionAction",
    "VersionDecision",
    "VersionGuard",
    "ViewDiff",
    "check_version",
    "default_projections",
    "diff_rows",
    "handles",
    "slot_namespace",
    "worst_lag",
]
