"""Observability for the CDC plane (kinora.md §12.5).

§12.5 asks the system to "emit, per shot / per session" a set of counters that
prove the pipeline works. The CDC plane has its own analogous set: end-to-end
**lag**, per-table **throughput**, **snapshot progress**, **dedup rate**, and
**view freshness** (how far a view's applied position trails the source head).
These feed the live metrics panel and post-hoc debugging exactly like the
buffer-occupancy timeline does for the scheduler.

:class:`CdcMetrics` is a small, dependency-free, thread-safe counter bag with a
:meth:`snapshot` that returns a plain dict (JSON-serialisable for a metrics
endpoint or a Prometheus exporter). It reads "now" through the package
:class:`~app.streaming.cdc.clock.Clock` so lag math is deterministic in tests.

A :class:`MeteredSink` decorator wraps any :class:`ChangeSink` to record
throughput + lag without the wrapped sink knowing, so metering is opt-in and
composable (drop it into a :class:`~app.streaming.cdc.sink.FanoutSink`).
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from app.streaming.cdc.clock import Clock, SystemClock
from app.streaming.cdc.events import ChangeEvent, LogPosition, Op
from app.streaming.cdc.sink import ChangeSink


@dataclass(slots=True)
class TableMetrics:
    """Per-table counters."""

    inserts: int = 0
    updates: int = 0
    deletes: int = 0
    reads: int = 0  # snapshot rows
    last_position_major: int = 0
    last_event_ts: float = 0.0

    @property
    def total(self) -> int:
        return self.inserts + self.updates + self.deletes + self.reads


class CdcMetrics:
    """Thread-safe counter bag for one connector."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._lock = threading.Lock()
        self._tables: dict[str, TableMetrics] = defaultdict(TableMetrics)
        self._heartbeats = 0
        self._schema_changes = 0
        self._deduped = 0
        self._errors = 0
        self._source_head = LogPosition.zero()
        self._max_lag_s = 0.0
        self._last_lag_s = 0.0

    # -- recording ---------------------------------------------------------- #
    def record_event(self, event: ChangeEvent) -> None:
        with self._lock:
            if event.op is Op.HEARTBEAT:
                self._heartbeats += 1
                return
            if event.op is Op.SCHEMA:
                self._schema_changes += 1
                return
            tm = self._tables[event.table]
            if event.op is Op.INSERT:
                tm.inserts += 1
            elif event.op is Op.UPDATE:
                tm.updates += 1
            elif event.op is Op.DELETE:
                tm.deletes += 1
            elif event.op is Op.READ:
                tm.reads += 1
            tm.last_position_major = max(tm.last_position_major, event.position.major)
            tm.last_event_ts = event.ts
            # End-to-end lag: how long between the source observing the change
            # and us processing it (clamped at 0 — clocks can disagree slightly).
            if event.ts:
                lag = max(0.0, self._clock.time() - event.ts)
                self._last_lag_s = lag
                self._max_lag_s = max(self._max_lag_s, lag)

    def record_dedup(self, n: int = 1) -> None:
        with self._lock:
            self._deduped += n

    def record_error(self, n: int = 1) -> None:
        with self._lock:
            self._errors += n

    def set_source_head(self, position: LogPosition) -> None:
        with self._lock:
            self._source_head = max(self._source_head, position)

    # -- reads -------------------------------------------------------------- #
    def view_lag(self, applied: LogPosition) -> int:
        """How far a view's applied position trails the source head (in major units)."""
        with self._lock:
            return max(0, self._source_head.major - applied.major)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "tables": {
                    name: {
                        "inserts": tm.inserts,
                        "updates": tm.updates,
                        "deletes": tm.deletes,
                        "reads": tm.reads,
                        "total": tm.total,
                        "last_position_major": tm.last_position_major,
                    }
                    for name, tm in self._tables.items()
                },
                "heartbeats": self._heartbeats,
                "schema_changes": self._schema_changes,
                "deduped": self._deduped,
                "errors": self._errors,
                "source_head_major": self._source_head.major,
                "last_lag_s": round(self._last_lag_s, 6),
                "max_lag_s": round(self._max_lag_s, 6),
                "total_events": sum(tm.total for tm in self._tables.values()),
            }


class MeteredSink:
    """Wrap a :class:`ChangeSink` to record metrics around each emit."""

    def __init__(self, inner: ChangeSink, metrics: CdcMetrics) -> None:
        self._inner = inner
        self._metrics = metrics

    async def emit(self, event: ChangeEvent) -> None:
        try:
            await self._inner.emit(event)
        except BaseException:
            self._metrics.record_error()
            raise
        self._metrics.record_event(event)
        self._metrics.set_source_head(event.position)


__all__ = ["CdcMetrics", "MeteredSink", "TableMetrics"]
