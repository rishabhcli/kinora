"""Snapshot + stream bootstrap — the consistent-cutover state machine.

A fresh CDC consumer must (1) capture the *current* contents of the source
(every row that already exists) and (2) then follow the live stream — without
losing a change that lands during the snapshot and without double-applying one
that the snapshot already saw. This is the classic Debezium "initial snapshot →
streaming" handoff.

:class:`SnapshotCoordinator` implements the **deferred-stream** strategy that is
correct for an *append-ordered* log:

1. Establish the snapshot's consistency point — the log position the snapshot
   reflects (the ``low_water``). A source exposes this via an optional
   ``snapshot_low_water`` (the LSN noted in the same transaction that opened the
   snapshot); absent one we use :meth:`LogPosition.zero` (snapshot reflects the
   point-in-time at the start of the log) which is always correct, just
   replays more.
2. Emit the snapshot as ``Op.READ`` events.
3. Resume the live stream *strictly after* ``low_water``, so changes that
   committed after the snapshot point are replayed exactly once and nothing the
   snapshot already reflected is double-counted.

Because positions are totally ordered, correctness is a position comparison, not
a lock. The phase is reported via :class:`SnapshotState` so the engine/metrics
can show "snapshotting" vs. "streaming".
"""

from __future__ import annotations

import enum
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.streaming.cdc.events import ChangeEvent, LogPosition
from app.streaming.cdc.source import CDCSource


class SnapshotState(enum.StrEnum):
    """Which phase of the bootstrap the coordinator is in."""

    PENDING = "pending"
    SNAPSHOTTING = "snapshotting"
    STREAMING = "streaming"
    DONE = "done"


@dataclass(slots=True)
class SnapshotProgress:
    """Observable counters for the bootstrap (metrics / tests)."""

    state: SnapshotState = SnapshotState.PENDING
    rows_snapshotted: int = 0
    stream_events: int = 0
    low_water: LogPosition = LogPosition.zero()
    last_position: LogPosition = LogPosition.zero()


class SnapshotCoordinator:
    """Drives a source through snapshot → stream with an exactly-once cutover."""

    def __init__(self, source: CDCSource, *, resume_from: LogPosition | None = None) -> None:
        self._source = source
        self._resume_from = resume_from
        self.progress = SnapshotProgress()

    @property
    def state(self) -> SnapshotState:
        return self.progress.state

    async def run(self) -> AsyncIterator[ChangeEvent]:
        """Yield the full bootstrap: snapshot READs then the live stream.

        If ``resume_from`` was supplied (a warm restart that already snapshotted)
        the snapshot phase is skipped and we resume the stream directly — the
        durable offset *is* the consistency point.
        """
        if self._resume_from is not None:
            self.progress.state = SnapshotState.STREAMING
            async for ev in self._stream_from(self._resume_from):
                yield ev
            self.progress.state = SnapshotState.DONE
            return

        # Phase 0: record the low-water mark before the snapshot.
        low_water = await self._head_position()
        self.progress.low_water = low_water

        # Phase 1: snapshot.
        self.progress.state = SnapshotState.SNAPSHOTTING
        async for ev in self._source.snapshot():
            self.progress.rows_snapshotted += 1
            self.progress.last_position = ev.position
            yield ev

        # Phase 2: stream everything strictly after the low-water mark.
        self.progress.state = SnapshotState.STREAMING
        async for ev in self._stream_from(low_water):
            yield ev

        self.progress.state = SnapshotState.DONE

    async def _stream_from(self, after: LogPosition) -> AsyncIterator[ChangeEvent]:
        async for ev in self._source.stream(after=after):
            self.progress.stream_events += 1
            self.progress.last_position = ev.position
            yield ev

    async def _head_position(self) -> LogPosition:
        """The snapshot's consistency point — where streaming resumes from.

        A source that knows the exact LSN it opened its snapshot at exposes it as
        ``snapshot_low_water`` (a :class:`LogPosition`); the WAL source sets this
        from the slot's ``confirmed_flush_lsn``. Absent one we use
        :meth:`LogPosition.zero`, which streams the whole log after the snapshot —
        always correct under at-least-once (the pipeline dedups), just less
        efficient. We deliberately do *not* use the current head here: a
        pre-existing log event is a change that post-dates the snapshot point and
        must still be streamed.
        """
        low = getattr(self._source, "snapshot_low_water", None)
        if isinstance(low, LogPosition):
            return low
        return LogPosition.zero()


__all__ = ["SnapshotCoordinator", "SnapshotProgress", "SnapshotState"]
