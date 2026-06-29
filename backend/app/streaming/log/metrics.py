"""Lightweight metrics for the streaming log — counters + a pluggable sink.

The brokers can be handed a :class:`MetricsSink` so operators see produce/fetch/
commit/rebalance/cleanup activity without the log depending on any particular
telemetry backend. The default :class:`InMemoryMetrics` keeps cumulative counters
(perfect for tests + a ``/metrics`` JSON endpoint); a Prometheus or
``app.observability`` adapter can implement the same tiny protocol.

The sink protocol is two methods — ``incr`` (a monotonic counter) and ``observe``
(a value sample, e.g. fetch batch size) — so it is trivial to back by anything.
Metric names follow a Kafka-ish convention: ``records_produced``,
``records_fetched``, ``fetch_requests``, ``offset_commits``, ``rebalances``,
``records_cleaned``, keyed by topic where useful.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "InMemoryMetrics",
    "MetricsSink",
    "NullMetrics",
    "MetricSnapshot",
]


@runtime_checkable
class MetricsSink(Protocol):
    """Where the log emits counters + observations."""

    def incr(self, name: str, value: int = 1, **labels: str) -> None:
        """Add ``value`` to the counter ``name`` (optionally label-scoped)."""
        ...

    def observe(self, name: str, value: float, **labels: str) -> None:
        """Record a value sample for ``name`` (e.g. a fetch batch size)."""
        ...


class NullMetrics:
    """A no-op sink — the default so metrics cost nothing when unused."""

    def incr(self, name: str, value: int = 1, **labels: str) -> None:  # noqa: D102
        return None

    def observe(self, name: str, value: float, **labels: str) -> None:  # noqa: D102
        return None


def _key(name: str, labels: dict[str, str]) -> str:
    if not labels:
        return name
    suffix = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{suffix}}}"


@dataclass(slots=True)
class MetricSnapshot:
    """An immutable view of the in-memory metrics at a point in time."""

    counters: dict[str, int]
    observations: dict[str, tuple[int, float]]  # name -> (count, sum)

    def counter(self, name: str, **labels: str) -> int:
        """Read a counter (0 if never incremented)."""
        return self.counters.get(_key(name, labels), 0)

    def mean(self, name: str, **labels: str) -> float:
        """Mean of the observations recorded for ``name`` (0.0 if none)."""
        count, total = self.observations.get(_key(name, labels), (0, 0.0))
        return total / count if count else 0.0


@dataclass(slots=True)
class InMemoryMetrics:
    """A cumulative in-process sink with a snapshot for inspection/serialization."""

    _counters: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _obs: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    def incr(self, name: str, value: int = 1, **labels: str) -> None:
        self._counters[_key(name, labels)] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        self._obs[_key(name, labels)].append(value)

    def snapshot(self) -> MetricSnapshot:
        """A read-only copy of the current counters + observation aggregates."""
        observations = {k: (len(v), sum(v)) for k, v in self._obs.items()}
        return MetricSnapshot(counters=dict(self._counters), observations=observations)

    def reset(self) -> None:
        """Clear all counters + observations (test convenience)."""
        self._counters.clear()
        self._obs.clear()
