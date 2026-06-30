"""Default observability sinks: a ``structlog`` event sink and recording sinks.

The engine emits :class:`~app.video.jobs.events.JobEvent`s and metrics through
the :class:`~app.video.jobs.ports.EventSink` / :class:`~app.video.jobs.ports.MetricsSink`
protocols. Production wires :class:`StructlogEventSink` (one structured log line
per lifecycle step) and any Prometheus-shaped :class:`~app.video.jobs.ports.MetricsSink`.
Tests use :class:`RecordingEventSink` / :class:`RecordingMetricsSink` and assert
on the captured sequence. :class:`NullMetricsSink` is the do-nothing default.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import structlog

from .events import JobEvent

_log = structlog.get_logger("video.jobs")


class StructlogEventSink:
    """Forwards every lifecycle event to ``structlog`` at info level (log-safe)."""

    def emit(self, event: JobEvent) -> None:
        _log.info(event.type.value, **event.as_log_fields())


class RecordingEventSink:
    """Captures emitted events in order for assertions in tests."""

    def __init__(self) -> None:
        self.events: list[JobEvent] = []

    def emit(self, event: JobEvent) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        """The ordered list of event-type values (handy for sequence assertions)."""
        return [e.type.value for e in self.events]


class NullMetricsSink:
    """A metrics sink that discards everything (the engine default)."""

    def incr(self, name: str, *, value: int = 1, **labels: str) -> None:  # noqa: D102
        return None

    def observe(self, name: str, value: float, **labels: str) -> None:  # noqa: D102
        return None


@dataclass
class RecordingMetricsSink:
    """Captures counters + observations for assertions in tests."""

    counters: Counter[str] = field(default_factory=Counter)
    observations: dict[str, list[float]] = field(default_factory=dict)

    def incr(self, name: str, *, value: int = 1, **labels: str) -> None:
        self.counters[name] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        self.observations.setdefault(name, []).append(value)


__all__ = [
    "NullMetricsSink",
    "RecordingEventSink",
    "RecordingMetricsSink",
    "StructlogEventSink",
]
