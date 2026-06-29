"""The projection runtime — catch-up, live tail, at-least-once + idempotent.

:class:`ProjectionRuntime` drives one :class:`Projection` against the consumed
:class:`EventStore`, persisting progress via a :class:`CheckpointStore` and
folding events into a :class:`ReadModelStore`. It implements the delivery
semantics the design promises:

**Catch-up.** :meth:`catch_up` pages the global stream forward from the stored
checkpoint in batches of ``batch_size``, applying each event and advancing the
checkpoint, until it reaches the store head. It returns when there is nothing
left to read, so a one-shot rebuild / cold start is just ``await catch_up()``.

**Live tail.** :meth:`run` first catches up, then subscribes to the store's tail
and applies events as they arrive, forever, until cancelled. This is the
long-lived task an operator runs per projection.

**At-least-once + idempotent.** Each event is processed inside
:meth:`_apply_one`, which (1) skips the event if the checkpoint store says this
``event_id`` was already applied (dedupe — survives a crash between apply and
checkpoint advance), (2) invokes ``projection.apply`` with retry, (3) marks the
event applied, then (4) advances the checkpoint. The ordering means a crash at
any step replays at most that one event and the dedupe drops the re-delivery.

**Error handling.** A handler exception is retried up to ``max_retries`` with a
fixed backoff; on exhaustion the runtime records the error on the checkpoint
(status → FAULTED) and either stops (``stop_on_error=True``, the safe default —
a poisoned event must not be silently skipped) or skips the event
(``stop_on_error=False``) so a non-critical projection can make progress. An
optional ``dead_letter`` sink captures skipped events for later inspection.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.eventsourcing.projections.checkpoints import (
    CheckpointStore,
    ProjectionCheckpoint,
    ProjectionStatus,
)
from app.eventsourcing.projections.contracts import EventStore, StoredEvent
from app.eventsourcing.projections.projection import Projection
from app.eventsourcing.projections.readmodel import ReadModelStore
from app.eventsourcing.projections.snapshots import (
    SnapshotPolicy,
    SnapshotStore,
    capture,
    restore_into,
)

logger = logging.getLogger(__name__)

#: A dead-letter sink receives (projection_name, event, error) for skipped events.
DeadLetterSink = Callable[[str, StoredEvent, Exception], Awaitable[None]]


@dataclass(slots=True)
class RuntimeConfig:
    """Tunables for one runtime instance."""

    batch_size: int = 256
    max_retries: int = 3
    retry_backoff_s: float = 0.05
    stop_on_error: bool = True
    poll_interval_s: float = 0.25


@dataclass(slots=True)
class CatchUpResult:
    """What a :meth:`ProjectionRuntime.catch_up` pass did."""

    applied: int = 0
    skipped: int = 0
    dead_lettered: int = 0
    final_position: int = 0
    faulted: bool = False
    #: Snapshots taken during this pass (0 unless a snapshot store + policy wired).
    snapshots: int = 0
    #: True when a rebuild restored from a snapshot instead of full-replaying.
    restored_from_snapshot: bool = False
    extra: dict[str, int] = field(default_factory=dict)


class ProjectionFaultedError(RuntimeError):
    """Raised by :meth:`catch_up` when a projection faults under ``stop_on_error``."""

    def __init__(self, projection: str, event: StoredEvent, cause: Exception) -> None:
        super().__init__(
            f"projection {projection!r} faulted on event {event.event_id!r} "
            f"(type={event.type!r}, position={event.global_position}): {cause}"
        )
        self.projection = projection
        self.event = event
        self.cause = cause


class ProjectionRuntime:
    """Drives one projection over the event log with checkpointed at-least-once delivery."""

    def __init__(
        self,
        projection: Projection,
        *,
        event_store: EventStore,
        read_models: ReadModelStore,
        checkpoints: CheckpointStore,
        namespace: str | None = None,
        config: RuntimeConfig | None = None,
        dead_letter: DeadLetterSink | None = None,
        snapshots: SnapshotStore | None = None,
        snapshot_policy: SnapshotPolicy | None = None,
    ) -> None:
        if not projection.name:
            raise ValueError("Projection.name must be set to a stable, unique value")
        self._projection = projection
        self._events = event_store
        self._read_models = read_models
        self._checkpoints = checkpoints
        #: The read-model namespace to write into (overridable for blue/green slots).
        self._namespace = namespace if namespace is not None else projection.namespace
        self._config = config or RuntimeConfig()
        self._dead_letter = dead_letter
        #: Optional snapshotting (replay acceleration). Off unless both are wired.
        self._snapshots = snapshots
        self._snapshot_policy = snapshot_policy or SnapshotPolicy()
        self._types = projection.interested_in()

    @property
    def projection_name(self) -> str:
        return self._projection.name

    @property
    def namespace(self) -> str:
        return self._namespace

    async def checkpoint(self) -> ProjectionCheckpoint:
        """The projection's current durable checkpoint."""
        return await self._checkpoints.load(self._projection.name)

    async def catch_up(self) -> CatchUpResult:
        """Apply every event from the checkpoint forward until reaching head.

        Returns a summary. Raises :class:`ProjectionFaultedError` if a handler exhausts
        retries while ``stop_on_error`` is set.
        """
        result = CatchUpResult()
        cp = await self._checkpoints.load(self._projection.name)
        position = cp.position
        head = await self._events.head_position()
        await self._checkpoints.advance(
            self._projection.name,
            position,
            status=ProjectionStatus.CATCHING_UP,
            observed_head=head,
        )
        type_filter = tuple(self._types) if self._types is not None else None
        applied_since_snapshot = 0
        while True:
            # Snapshot the head *before* reading the batch: any event at or below
            # this head that the filter skipped is safe to checkpoint past once the
            # filtered batch drains. Events appended after this read carry a higher
            # position and are picked up on the next loop / by the live tail.
            head_before_read = await self._events.head_position()
            batch = await self._events.read_all(
                after_position=position,
                limit=self._config.batch_size,
                types=type_filter,
            )
            if not batch:
                position = max(position, head_before_read)
                break
            for event in batch:
                outcome = await self._apply_one(event)
                if outcome == _Outcome.APPLIED:
                    result.applied += 1
                    applied_since_snapshot += 1
                elif outcome == _Outcome.SKIPPED_DEDUPE:
                    result.skipped += 1
                elif outcome in (_Outcome.DEAD_LETTERED, _Outcome.SKIPPED_ERROR):
                    result.dead_lettered += 1
                    result.faulted = True
                # Position always advances to the event we just processed, even on
                # a dedupe-skip — the checkpoint should track the stream, not the
                # subset we mutated.
                position = event.global_position
                await self._checkpoints.advance(self._projection.name, position)
            # Opportunistic snapshot between batches per the policy (off by default).
            if self._snapshot_policy.should_snapshot(applied_since_snapshot):
                await self._maybe_snapshot(position)
                result.snapshots += 1
                applied_since_snapshot = 0
        # The filtered stream is drained and ``position`` was advanced to the head
        # observed just before the empty read (above), so a type-filtered
        # projection reports lag 0 when caught up rather than counting the events
        # it deliberately skipped. Re-read head only for the observed_head health
        # field (it may have grown during the drain; those events are the tail's).
        head = await self._events.head_position()
        await self._checkpoints.advance(
            self._projection.name,
            position,
            status=ProjectionStatus.LIVE,
            observed_head=head,
        )
        result.final_position = position
        return result

    async def run(self, *, stop_event: asyncio.Event | None = None) -> None:
        """Catch up, then tail the live stream forever (until cancelled / stopped)."""
        await self.catch_up()
        cp = await self._checkpoints.load(self._projection.name)
        subscription = self._events.subscribe(
            after_position=cp.position,
            poll_interval_s=self._config.poll_interval_s,
        )
        try:
            async for event in subscription:
                if stop_event is not None and stop_event.is_set():
                    break
                # In live tail mode a fault under stop_on_error raises out of the
                # loop; the non-fatal modes skip and keep tailing.
                await self._apply_one(event)
                await self._checkpoints.advance(
                    self._projection.name,
                    event.global_position,
                    observed_head=event.global_position,
                )
        finally:
            aclose = getattr(subscription, "aclose", None)
            if aclose is not None:
                await aclose()

    async def rebuild(self) -> CatchUpResult:
        """Reset the checkpoint + clear the namespace, then catch up from scratch.

        This is the in-place rebuild (no blue/green slot). The namespace is cleared
        first so a fold that only ever upserts cannot leave orphaned rows behind.
        Always replays the *whole* log — use :meth:`restore_or_rebuild` for the
        snapshot-accelerated path.
        """
        await self._projection.on_reset(self._read_models, self._namespace)
        await self._read_models.clear(self._namespace)
        await self._checkpoints.reset(self._projection.name)
        return await self.catch_up()

    async def restore_or_rebuild(self) -> CatchUpResult:
        """Restore the latest valid snapshot then replay the tail; else full rebuild.

        A snapshot is *valid* only if it was captured under the projection's current
        fold ``version`` — a version bump invalidates older snapshots (the fold
        changed), so we fall back to a full rebuild. With no snapshot store wired
        this is exactly :meth:`rebuild`.
        """
        if self._snapshots is None:
            return await self.rebuild()
        snapshot = await self._snapshots.latest(self._projection.name)
        if snapshot is None or snapshot.projection_version != self._projection.version:
            return await self.rebuild()
        # Restore the captured rows, then resume the checkpoint at the snapshot
        # position so catch_up replays only the tail. The applied-event ledger is
        # reset so tail events re-apply cleanly (they are all > snapshot.position).
        await self._read_models.clear(self._namespace)
        restored = await restore_into(self._read_models, self._namespace, snapshot)
        await self._checkpoints.reset(self._projection.name)
        await self._checkpoints.advance(
            self._projection.name,
            snapshot.position,
            status=ProjectionStatus.CATCHING_UP,
        )
        result = await self.catch_up()
        result.restored_from_snapshot = True
        result.extra["restored_rows"] = restored
        return result

    async def snapshot_now(self) -> int:
        """Capture a snapshot at the current checkpoint position; return row count.

        No-op (returns 0) when no snapshot store is wired. Useful for an explicit
        "snapshot before deploy" hook in addition to the policy-driven captures.
        """
        cp = await self._checkpoints.load(self._projection.name)
        return await self._maybe_snapshot(cp.position)

    async def _maybe_snapshot(self, position: int) -> int:
        if self._snapshots is None:
            return 0
        snapshot = await capture(
            self._read_models,
            projection=self._projection.name,
            namespace=self._namespace,
            position=position,
            projection_version=self._projection.version,
        )
        await self._snapshots.save(snapshot)
        return snapshot.row_count

    # -- internals ----------------------------------------------------------- #

    async def _apply_one(self, event: StoredEvent) -> _Outcome:
        # (1) Idempotency: skip an already-applied event (at-least-once dedupe).
        newly = await self._checkpoints.mark_applied(self._projection.name, event.event_id)
        if not newly:
            return _Outcome.SKIPPED_DEDUPE
        # (2) Apply with retry.
        last_exc: Exception | None = None
        for attempt in range(self._config.max_retries):
            try:
                await self._projection.apply(self._read_models, self._namespace, event)
                return _Outcome.APPLIED
            except Exception as exc:  # noqa: BLE001 - runtime is the error boundary
                last_exc = exc
                logger.warning(
                    "projection %s failed on event %s (attempt %d/%d): %s",
                    self._projection.name,
                    event.event_id,
                    attempt + 1,
                    self._config.max_retries,
                    exc,
                )
                if attempt + 1 < self._config.max_retries:
                    await asyncio.sleep(self._config.retry_backoff_s)
        assert last_exc is not None
        await self._checkpoints.record_error(self._projection.name, str(last_exc))
        if self._config.stop_on_error:
            raise ProjectionFaultedError(self._projection.name, event, last_exc)
        # Non-fatal mode: dead-letter (if a sink is wired) and skip the event so the
        # projection keeps making forward progress past a single poison message.
        if self._dead_letter is not None:
            await self._dead_letter(self._projection.name, event, last_exc)
            return _Outcome.DEAD_LETTERED
        return _Outcome.SKIPPED_ERROR


class _Outcome(enum.Enum):
    """Result codes for :meth:`ProjectionRuntime._apply_one`."""

    APPLIED = "applied"
    SKIPPED_DEDUPE = "skipped_dedupe"
    DEAD_LETTERED = "dead_lettered"
    SKIPPED_ERROR = "skipped_error"


__all__ = [
    "CatchUpResult",
    "DeadLetterSink",
    "ProjectionFaultedError",
    "ProjectionRuntime",
    "RuntimeConfig",
]
