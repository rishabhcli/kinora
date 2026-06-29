"""Checkpoint + applied-event tracking — the spine of at-least-once delivery.

The runtime processes the global event stream in ``global_position`` order and,
after each event (or batch) is folded into the read model, records the new
position here. On restart it resumes from the stored checkpoint. Because the
write to the read model and the checkpoint advance are **not** a single
distributed transaction in the general case, delivery is *at-least-once*: a
crash after applying an event but before advancing the checkpoint replays that
event.

Two mechanisms make replay safe:

1. **Position monotonicity.** :meth:`CheckpointStore.advance` only moves a
   checkpoint *forward*; a stale/duplicate advance is a no-op. The stored value
   is the highest fully-applied ``global_position``.

2. **Applied-event dedupe (idempotency).** :meth:`CheckpointStore.mark_applied`
   records that ``(projection, event_id)`` was applied and returns whether it
   was *newly* recorded. The runtime calls it before invoking a handler and
   **skips** an already-applied event. This is what lets a relative handler
   ("increment read count") survive at-least-once delivery: the second delivery
   is dropped before the handler runs.

A :class:`ProjectionCheckpoint` also carries liveness/health: the head position
the runtime last observed (for lag math, :mod:`.lag`), an error count, the last
error string, and a status. The runtime updates these as it goes so an operator
(and the blue/green orchestrator) can see whether a projection is caught up,
lagging, or faulted.

The in-memory implementation here is deterministic and dependency-free; the
Postgres store lives in :mod:`app.eventsourcing.projections.checkpoints_pg`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from app.eventsourcing.projections.contracts import NO_POSITION, GlobalPosition


class ProjectionStatus(enum.StrEnum):
    """Lifecycle/health of a single projection's checkpoint."""

    #: Created, never run, or actively replaying from the start.
    CATCHING_UP = "catching_up"
    #: Caught up to head and tailing live appends.
    LIVE = "live"
    #: Hit the retry ceiling on an event; halted, needs intervention/reset.
    FAULTED = "faulted"
    #: Administratively paused (e.g. during a blue/green swap).
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class ProjectionCheckpoint:
    """The durable position + health record for one projection (one slot)."""

    projection: str
    position: GlobalPosition = NO_POSITION
    status: ProjectionStatus = ProjectionStatus.CATCHING_UP
    #: The head position the runtime last observed (for lag = head - position).
    observed_head: GlobalPosition = NO_POSITION
    error_count: int = 0
    last_error: str | None = None
    #: Bump-on-incompatible-change marker copied from the projection (see rebuilds).
    projection_version: int = 1
    updated_at: datetime | None = None

    @property
    def lag(self) -> int:
        """How many positions behind the observed head this checkpoint is (≥ 0)."""
        return max(0, self.observed_head - self.position)

    @property
    def is_caught_up(self) -> bool:
        """True when the checkpoint has reached the last observed head."""
        return self.position >= self.observed_head


@runtime_checkable
class CheckpointStore(Protocol):
    """Durable position tracking + applied-event dedupe for projections."""

    async def load(self, projection: str) -> ProjectionCheckpoint:
        """Return the checkpoint for ``projection`` (a fresh zero one if absent)."""
        ...

    async def advance(
        self,
        projection: str,
        position: GlobalPosition,
        *,
        status: ProjectionStatus | None = None,
        observed_head: GlobalPosition | None = None,
    ) -> ProjectionCheckpoint:
        """Move the checkpoint forward to ``position`` (no-op if not greater).

        Optionally updates ``status`` and ``observed_head`` regardless of whether
        the position moved (so health can be refreshed while idle/caught-up).
        """
        ...

    async def record_error(self, projection: str, error: str) -> ProjectionCheckpoint:
        """Increment the error count, store ``error``, set status FAULTED."""
        ...

    async def set_status(self, projection: str, status: ProjectionStatus) -> ProjectionCheckpoint:
        """Set the projection's status (e.g. PAUSED / LIVE) without moving position."""
        ...

    async def reset(self, projection: str) -> ProjectionCheckpoint:
        """Reset to position 0 / CATCHING_UP and drop the applied-event set (rebuild)."""
        ...

    async def set_projection_version(
        self, projection: str, version: int
    ) -> ProjectionCheckpoint:
        """Record the fold version that produced the current view (version guard)."""
        ...

    async def mark_applied(self, projection: str, event_id: str) -> bool:
        """Record that ``event_id`` was applied; return True if newly recorded."""
        ...

    async def was_applied(self, projection: str, event_id: str) -> bool:
        """Whether ``event_id`` has already been applied by ``projection``."""
        ...


class InMemoryCheckpointStore:
    """A deterministic, in-process :class:`CheckpointStore`.

    Holds one :class:`ProjectionCheckpoint` per projection plus an applied-event
    id set per projection. The applied-set is unbounded in this fake (fine for
    tests); the Postgres store prunes it below the committed position because an
    event at or before the checkpoint can never be re-delivered out of order.
    """

    def __init__(self) -> None:
        self._checkpoints: dict[str, ProjectionCheckpoint] = {}
        self._applied: dict[str, set[str]] = {}

    async def load(self, projection: str) -> ProjectionCheckpoint:
        return self._checkpoints.get(projection) or ProjectionCheckpoint(projection=projection)

    async def advance(
        self,
        projection: str,
        position: GlobalPosition,
        *,
        status: ProjectionStatus | None = None,
        observed_head: GlobalPosition | None = None,
    ) -> ProjectionCheckpoint:
        cp = await self.load(projection)
        new_position = max(cp.position, position)
        cp = replace(
            cp,
            position=new_position,
            status=status if status is not None else cp.status,
            observed_head=(
                max(cp.observed_head, observed_head)
                if observed_head is not None
                else max(cp.observed_head, new_position)
            ),
            updated_at=datetime.now(UTC),
        )
        self._checkpoints[projection] = cp
        return cp

    async def record_error(self, projection: str, error: str) -> ProjectionCheckpoint:
        cp = await self.load(projection)
        cp = replace(
            cp,
            error_count=cp.error_count + 1,
            last_error=error,
            status=ProjectionStatus.FAULTED,
            updated_at=datetime.now(UTC),
        )
        self._checkpoints[projection] = cp
        return cp

    async def set_status(
        self, projection: str, status: ProjectionStatus
    ) -> ProjectionCheckpoint:
        cp = await self.load(projection)
        cp = replace(cp, status=status, updated_at=datetime.now(UTC))
        self._checkpoints[projection] = cp
        return cp

    async def reset(self, projection: str) -> ProjectionCheckpoint:
        prior = self._checkpoints.get(projection)
        cp = ProjectionCheckpoint(
            projection=projection,
            status=ProjectionStatus.CATCHING_UP,
            # Preserve the recorded fold version across a reset so a rebuild keeps
            # the version it is rebuilding *for* even if interrupted mid-replay.
            projection_version=prior.projection_version if prior is not None else 1,
            updated_at=datetime.now(UTC),
        )
        self._checkpoints[projection] = cp
        self._applied.pop(projection, None)
        return cp

    async def set_projection_version(
        self, projection: str, version: int
    ) -> ProjectionCheckpoint:
        cp = await self.load(projection)
        cp = replace(cp, projection_version=version, updated_at=datetime.now(UTC))
        self._checkpoints[projection] = cp
        return cp

    async def mark_applied(self, projection: str, event_id: str) -> bool:
        seen = self._applied.setdefault(projection, set())
        if event_id in seen:
            return False
        seen.add(event_id)
        return True

    async def was_applied(self, projection: str, event_id: str) -> bool:
        return event_id in self._applied.get(projection, set())

    # -- inspection ---------------------------------------------------------- #

    def all_checkpoints(self) -> list[ProjectionCheckpoint]:
        return [self._checkpoints[k] for k in sorted(self._checkpoints)]


@dataclass(slots=True)
class _MutableCounters:
    """Internal helper used by tests to assert applied-set growth (not exported)."""

    applied: int = 0
    skipped: int = 0
    extra: dict[str, int] = field(default_factory=dict)


__all__ = [
    "CheckpointStore",
    "InMemoryCheckpointStore",
    "ProjectionCheckpoint",
    "ProjectionStatus",
]
