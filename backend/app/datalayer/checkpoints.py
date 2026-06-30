"""Checkpoint + applied-event tracking — the spine of at-least-once delivery.

A :class:`ProjectionRunner` consumes the global log in ``global_position`` order
(the ordering :class:`~app.eventsourcing.store.contracts.EventStore` exposes via
``read_all`` / ``last_position``) and, after folding an event into the read
model, records the new position here. On restart it resumes from the stored
checkpoint.

Positions follow the store's contract exactly: a ``global_position`` is 1-based
and dense; ``position == 0`` means "nothing consumed yet" (replay the whole log);
``position == N`` means "every event with ``global_position <= N`` is applied",
so the runner resumes by reading ``read_all(from_position=N)`` (exclusive).

Because the read-model write and the checkpoint advance are not one distributed
transaction in the general case, delivery is **at-least-once**. Two mechanisms
make replay safe:

1. **Position monotonicity** — :meth:`CheckpointStore.advance` only moves a
   checkpoint forward; a stale advance is a no-op.
2. **Applied-event dedupe** — :meth:`CheckpointStore.mark_applied` records that
   ``(projection, event_id)`` was applied and returns whether it was *newly*
   recorded; the runner skips an already-applied event before its handler runs,
   so even a relative ("increment") handler survives redelivery.

A :class:`ProjectionCheckpoint` also carries liveness: the head position last
observed (for lag math), an error count + last error, a status, and the fold
``projection_version`` that produced the current view (a version bump triggers a
rebuild). The in-memory store here is deterministic and dependency-free; the
Postgres store backs the ``datalayer_checkpoints`` / ``datalayer_applied`` tables
(:mod:`app.datalayer.models`).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

#: A global position over the whole log. 0 == "before the first event"; the first
#: appended event is at position 1 (matches the store's dense, 1-based ordering).
GlobalPosition = int

#: The conventional "nothing consumed yet" checkpoint value.
NO_POSITION: GlobalPosition = 0


class ProjectionStatus(enum.StrEnum):
    """Lifecycle / health of a single projection's checkpoint."""

    #: Created, never run, or actively replaying from the start.
    CATCHING_UP = "catching_up"
    #: Caught up to the last observed head.
    LIVE = "live"
    #: Hit the retry ceiling on an event; halted, needs a reset/rebuild.
    FAULTED = "faulted"
    #: Administratively paused (e.g. during a rebuild swap).
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class ProjectionCheckpoint:
    """The durable position + health record for one projection."""

    projection: str
    position: GlobalPosition = NO_POSITION
    status: ProjectionStatus = ProjectionStatus.CATCHING_UP
    #: The head position the runner last observed (lag = observed_head - position).
    observed_head: GlobalPosition = NO_POSITION
    events_applied: int = 0
    error_count: int = 0
    last_error: str | None = None
    #: Fold version that produced the current view; a bump forces a rebuild.
    projection_version: int = 1
    updated_at: datetime | None = None

    @property
    def lag(self) -> int:
        """How many positions behind the observed head this checkpoint is (>= 0)."""
        return max(0, self.observed_head - self.position)

    @property
    def is_caught_up(self) -> bool:
        """True once the position has reached the last observed head."""
        return self.position >= self.observed_head


@runtime_checkable
class CheckpointStore(Protocol):
    """Durable position tracking + applied-event dedupe for projections."""

    async def load(self, projection: str) -> ProjectionCheckpoint:
        """Return ``projection``'s checkpoint (a fresh zero one if absent)."""
        ...

    async def advance(
        self,
        projection: str,
        position: GlobalPosition,
        *,
        applied_delta: int = 0,
        status: ProjectionStatus | None = None,
        observed_head: GlobalPosition | None = None,
    ) -> ProjectionCheckpoint:
        """Move the checkpoint forward to ``position`` (no-op if not greater).

        ``applied_delta`` bumps the events-applied counter; ``status`` /
        ``observed_head`` refresh health regardless of whether the position moved.
        """
        ...

    async def record_error(self, projection: str, error: str) -> ProjectionCheckpoint:
        """Increment the error count, store ``error``, set status FAULTED."""
        ...

    async def set_status(self, projection: str, status: ProjectionStatus) -> ProjectionCheckpoint:
        """Set the status (e.g. PAUSED / LIVE) without moving the position."""
        ...

    async def set_projection_version(
        self, projection: str, version: int
    ) -> ProjectionCheckpoint:
        """Record the fold version that produced the current view."""
        ...

    async def reset(self, projection: str) -> ProjectionCheckpoint:
        """Reset to position 0 / CATCHING_UP and drop the applied set (rebuild)."""
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
    id set per projection. The applied set is unbounded here (fine for tests); the
    Postgres store prunes ids at/below the committed position, since an event
    there can never be re-delivered out of order.
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
        applied_delta: int = 0,
        status: ProjectionStatus | None = None,
        observed_head: GlobalPosition | None = None,
    ) -> ProjectionCheckpoint:
        cp = await self.load(projection)
        new_position = max(cp.position, position)
        cp = replace(
            cp,
            position=new_position,
            events_applied=cp.events_applied + max(0, applied_delta),
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

    async def set_status(self, projection: str, status: ProjectionStatus) -> ProjectionCheckpoint:
        cp = await self.load(projection)
        cp = replace(cp, status=status, updated_at=datetime.now(UTC))
        self._checkpoints[projection] = cp
        return cp

    async def set_projection_version(
        self, projection: str, version: int
    ) -> ProjectionCheckpoint:
        cp = await self.load(projection)
        cp = replace(cp, projection_version=version, updated_at=datetime.now(UTC))
        self._checkpoints[projection] = cp
        return cp

    async def reset(self, projection: str) -> ProjectionCheckpoint:
        prior = self._checkpoints.get(projection)
        cp = ProjectionCheckpoint(
            projection=projection,
            status=ProjectionStatus.CATCHING_UP,
            # Preserve the recorded fold version across a reset so an interrupted
            # rebuild keeps the version it is rebuilding *for*.
            projection_version=prior.projection_version if prior is not None else 1,
            updated_at=datetime.now(UTC),
        )
        self._checkpoints[projection] = cp
        self._applied.pop(projection, None)
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


__all__ = [
    "NO_POSITION",
    "CheckpointStore",
    "GlobalPosition",
    "InMemoryCheckpointStore",
    "ProjectionCheckpoint",
    "ProjectionStatus",
]
