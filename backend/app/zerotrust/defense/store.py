"""The persistence seam for emitted alerts.

The detection engine is pure: it computes alerts and hands them to a
:class:`AlertSink`. *Where* alerts go (an in-memory ring for tests, Redis, a
Postgres table, a SIEM webhook) is a deployment choice expressed behind this
seam, so the engine never imports a database.

Two implementations ship here:

* :class:`InMemoryAlertStore` — a bounded ring buffer with query helpers; the
  default for tests and the off-network demo.
* :class:`NullAlertSink` — drops everything (a benchmark / dry-run sink).

The seam is deliberately synchronous: alert volume is tiny relative to event
volume (deduplication collapses storms), and a synchronous sink keeps the engine
trivially testable. An async/Redis adapter can wrap this without changing the
:class:`AlertSink` protocol.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator
from typing import Protocol, runtime_checkable

from .types import Alert, Severity, ThreatCategory


@runtime_checkable
class AlertSink(Protocol):
    """Where the engine writes alerts."""

    def record(self, alert: Alert) -> None:
        """Persist or forward one (already-deduplicated) alert."""


class NullAlertSink:
    """A sink that discards alerts (for benchmarks / dry runs)."""

    __slots__ = ()

    def record(self, alert: Alert) -> None:  # noqa: D401 - intentional no-op
        return None


class InMemoryAlertStore:
    """A bounded in-memory alert ring with simple query helpers.

    Newest alerts evict oldest once ``capacity`` is reached. The store keeps a
    by-dedup-key index of the *latest* version of each alert so callers can read
    the current rolled-up count without scanning the ring.
    """

    __slots__ = ("capacity", "_ring", "_latest", "_total")

    def __init__(self, capacity: int = 10_000) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._ring: deque[Alert] = deque(maxlen=capacity)
        self._latest: dict[str, Alert] = {}
        self._total = 0

    def record(self, alert: Alert) -> None:
        self._ring.append(alert)
        self._latest[alert.dedup_key] = alert
        self._total += 1

    def __len__(self) -> int:
        return len(self._ring)

    def __iter__(self) -> Iterator[Alert]:
        return iter(self._ring)

    @property
    def total_recorded(self) -> int:
        """Count of every ``record`` call, including evicted alerts."""
        return self._total

    def latest(self, dedup_key: str) -> Alert | None:
        return self._latest.get(dedup_key)

    def all(self) -> list[Alert]:
        return list(self._ring)

    def query(
        self,
        *,
        min_severity: Severity | None = None,
        category: ThreatCategory | None = None,
        subject: str | None = None,
        source_ip: str | None = None,
        since: float | None = None,
    ) -> list[Alert]:
        """Filter the ring by the common dimensions, newest-last."""

        def keep(a: Alert) -> bool:
            if min_severity is not None and a.severity < min_severity:
                return False
            if category is not None and a.category != category:
                return False
            if subject is not None and a.subject != subject:
                return False
            if source_ip is not None and a.source_ip != source_ip:
                return False
            return not (since is not None and a.last_at < since)

        return [a for a in self._ring if keep(a)]

    def top_subjects(self, *, limit: int = 10) -> list[tuple[str, int]]:
        """Subjects ranked by alert count (a quick triage list)."""
        counts: dict[str, int] = {}
        for a in self._ring:
            counts[a.subject] = counts.get(a.subject, 0) + a.count
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[:limit]

    def categories(self) -> dict[str, int]:
        """Histogram of alert counts by category label."""
        hist: dict[str, int] = {}
        for a in self._ring:
            label = str(a.category)
            hist[label] = hist.get(label, 0) + 1
        return hist

    def extend(self, alerts: Iterable[Alert]) -> None:
        for a in alerts:
            self.record(a)


__all__ = ["AlertSink", "InMemoryAlertStore", "NullAlertSink"]
