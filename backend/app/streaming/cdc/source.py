"""Change-data-capture sources — where events come from.

A :class:`CDCSource` is an async iterator of :class:`ChangeEvent`. Concrete
sources:

* :class:`FakeChangeStream` (this module) — a fully in-memory, deterministic
  stream for tests: you push rows/inserts/updates/deletes/snapshots and it hands
  them back in order with monotonically increasing positions. No threads, no
  timers, no infra.
* :class:`PostgresLogicalSource` (``wal_source.py``) — decodes a logical
  replication slot.
* :class:`PollingSource` (``polling_source.py``) — the updated-at + tombstone
  fallback when logical replication isn't available.

All three share the contract here, so the pipeline, snapshot bootstrap, and view
engine are written once against the ABC.
"""

from __future__ import annotations

import abc
from collections import deque
from collections.abc import AsyncIterator, Iterable

from app.streaming.cdc.clock import Clock, SystemClock
from app.streaming.cdc.events import ChangeEvent, JsonRow, LogPosition, Op


class CDCSource(abc.ABC):
    """An async, resumable, totally ordered stream of change events."""

    @abc.abstractmethod
    def stream(self, *, after: LogPosition | None = None) -> AsyncIterator[ChangeEvent]:
        """Yield change events strictly *after* ``after`` (exclusive), in order.

        ``after`` is the resume checkpoint; ``None`` means from the beginning.
        Implementations must never yield an event with ``position <= after``.
        """
        raise NotImplementedError

    async def snapshot(self) -> AsyncIterator[ChangeEvent]:  # pragma: no cover - default
        """Yield the initial consistent snapshot as ``Op.READ`` events.

        Default is empty (a pure-stream source). :class:`FakeChangeStream` and
        the polling source override this for snapshot+stream bootstrap.
        """
        return
        yield  # pragma: no cover - makes this an async generator


class FakeChangeStream(CDCSource):
    """A deterministic, in-memory change stream for tests.

    Build a script of events with the ``push_*`` helpers (or seed a snapshot),
    then iterate. Positions auto-increment by ``major`` so ordering is total and
    resume-after works exactly. Because there are no real timers it is safe to
    drive from a single test coroutine.
    """

    def __init__(self, *, clock: Clock | None = None, start_lsn: int = 1) -> None:
        self._clock = clock or SystemClock()
        self._lsn = start_lsn
        self._log: list[ChangeEvent] = []
        self._snapshot: list[ChangeEvent] = []

    # -- position helper ---------------------------------------------------- #
    def _next_position(self) -> LogPosition:
        pos = LogPosition(self._lsn, 0)
        self._lsn += 1
        return pos

    # -- snapshot seeding --------------------------------------------------- #
    def seed_snapshot(
        self,
        table: str,
        rows: Iterable[JsonRow],
        *,
        key_columns: tuple[str, ...] = ("id",),
        schema_version: int = 1,
    ) -> FakeChangeStream:
        """Register rows that the initial snapshot will replay as ``READ`` events.

        Snapshot rows take positions *below* any subsequently streamed change
        (they are assigned ``minor`` slots under ``major=0``) so a consumer that
        replays snapshot-then-stream sees a consistent, gap-free order.
        """
        for row in rows:
            # Snapshot rows live under major=0 with an incrementing minor so they
            # order before any streamed change (which starts at major>=1).
            self._snapshot.append(
                ChangeEvent.read(
                    table,
                    row,
                    LogPosition(0, len(self._snapshot)),
                    key_columns=key_columns,
                    ts=self._clock.time(),
                    schema_version=schema_version,
                )
            )
        return self

    # -- stream scripting --------------------------------------------------- #
    def push_insert(
        self,
        table: str,
        after: JsonRow,
        *,
        key_columns: tuple[str, ...] = ("id",),
        schema_version: int = 1,
        meta: JsonRow | None = None,
    ) -> ChangeEvent:
        ev = ChangeEvent.insert(
            table,
            after,
            self._next_position(),
            key_columns=key_columns,
            ts=self._clock.time(),
            schema_version=schema_version,
            meta=meta,
        )
        self._log.append(ev)
        return ev

    def push_update(
        self,
        table: str,
        before: JsonRow | None,
        after: JsonRow,
        *,
        key_columns: tuple[str, ...] = ("id",),
        schema_version: int = 1,
        meta: JsonRow | None = None,
    ) -> ChangeEvent:
        ev = ChangeEvent.update(
            table,
            before,
            after,
            self._next_position(),
            key_columns=key_columns,
            ts=self._clock.time(),
            schema_version=schema_version,
            meta=meta,
        )
        self._log.append(ev)
        return ev

    def push_delete(
        self,
        table: str,
        before: JsonRow,
        *,
        key_columns: tuple[str, ...] = ("id",),
        schema_version: int = 1,
        meta: JsonRow | None = None,
    ) -> ChangeEvent:
        ev = ChangeEvent.delete(
            table,
            before,
            self._next_position(),
            key_columns=key_columns,
            ts=self._clock.time(),
            schema_version=schema_version,
            meta=meta,
        )
        self._log.append(ev)
        return ev

    def push_schema(
        self,
        table: str,
        columns: JsonRow,
        *,
        schema_version: int,
        meta: JsonRow | None = None,
    ) -> ChangeEvent:
        ev = ChangeEvent.schema(
            table,
            columns,
            self._next_position(),
            schema_version=schema_version,
            ts=self._clock.time(),
            meta=meta,
        )
        self._log.append(ev)
        return ev

    def push_heartbeat(self) -> ChangeEvent:
        ev = ChangeEvent.heartbeat(self._next_position(), ts=self._clock.time())
        self._log.append(ev)
        return ev

    def push(self, event: ChangeEvent) -> ChangeEvent:
        """Append a pre-built event (its position is honoured as given)."""
        self._log.append(event)
        return event

    # -- CDCSource interface ----------------------------------------------- #
    async def snapshot(self) -> AsyncIterator[ChangeEvent]:
        for ev in self._snapshot:
            yield ev

    async def stream(self, *, after: LogPosition | None = None) -> AsyncIterator[ChangeEvent]:
        cutoff = after or LogPosition.zero()
        for ev in self._log:
            if ev.position > cutoff:
                yield ev

    # -- introspection ------------------------------------------------------ #
    @property
    def log(self) -> list[ChangeEvent]:
        return list(self._log)

    @property
    def head_position(self) -> LogPosition:
        return self._log[-1].position if self._log else LogPosition.zero()


class ReplayBuffer:
    """A bounded ring of recently emitted events for at-least-once redelivery.

    A source that can't cheaply re-read its log (e.g. a consumed socket) keeps a
    tail here so a consumer that crashed mid-batch can be re-served everything
    after its last committed offset without a full re-snapshot.
    """

    def __init__(self, capacity: int = 1024) -> None:
        self._buf: deque[ChangeEvent] = deque(maxlen=capacity)

    def append(self, event: ChangeEvent) -> None:
        self._buf.append(event)

    def replay_after(self, after: LogPosition) -> list[ChangeEvent]:
        return [e for e in self._buf if e.position > after]

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self._buf)


__all__ = ["CDCSource", "FakeChangeStream", "Op", "ReplayBuffer"]
