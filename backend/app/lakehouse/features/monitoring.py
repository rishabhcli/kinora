"""Feature-store monitoring — a self-contained telemetry aggregator.

Rather than scatter Prometheus calls through the hot path, the feature store
accumulates its operational signals into a small in-process :class:`FeatureMonitor`
(thread-safe counters + gauges + view-scoped freshness/parity/skew snapshots). The
monitor is a plain value sink — the API/observability layer can read a
:meth:`snapshot` and project it onto the app's Prometheus registry, or a test can
assert on it directly. This keeps the feature store free of a hard Prometheus
dependency while still being fully observable.

Signals tracked:

* serving: ``online_reads`` / ``online_hits`` (hit rate), ``online_misses``,
* materialisation: ``materializations`` / ``rows_materialized``,
* training: ``training_sets_built`` / ``training_rows``,
* quality: the latest parity match-rate and skew-drift count per feature view,
* freshness: the latest SLA-met fraction per feature view.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass

from .freshness import FreshnessReport
from .materialization import MaterializationResult
from .parity import ParityReport, SkewReport


@dataclass(slots=True)
class ViewHealth:
    """The latest quality/freshness snapshot for one feature view."""

    parity_match_rate: float | None = None
    skew_drifted: int | None = None
    freshness_sla: float | None = None
    rows_materialized: int = 0
    last_materialized_coverage: float | None = None


@dataclass(slots=True)
class MonitorSnapshot:
    counters: dict[str, int]
    view_health: dict[str, ViewHealth]

    @property
    def online_hit_rate(self) -> float:
        reads = self.counters.get("online_reads", 0)
        hits = self.counters.get("online_hits", 0)
        return 1.0 if reads == 0 else hits / reads


class FeatureMonitor:
    """Thread-safe accumulator of feature-store operational metrics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._views: dict[str, ViewHealth] = defaultdict(ViewHealth)

    # -- counters -------------------------------------------------------- #

    def incr(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def record_online_read(self, *, hit: bool) -> None:
        with self._lock:
            self._counters["online_reads"] += 1
            self._counters["online_hits" if hit else "online_misses"] += 1

    def record_training_set(self, rows: int) -> None:
        with self._lock:
            self._counters["training_sets_built"] += 1
            self._counters["training_rows"] += rows

    # -- view-scoped quality signals ------------------------------------- #

    def record_materialization(self, result: MaterializationResult) -> None:
        with self._lock:
            self._counters["materializations"] += 1
            self._counters["rows_materialized"] += result.rows_written
            health = self._views[result.view]
            health.rows_materialized += result.rows_written
            health.last_materialized_coverage = result.coverage

    def record_parity(self, view: str, report: ParityReport) -> None:
        with self._lock:
            self._views[view].parity_match_rate = report.overall_match_rate

    def record_skew(self, view: str, report: SkewReport) -> None:
        with self._lock:
            self._views[view].skew_drifted = len(report.drifted_features)

    def record_freshness(self, report: FreshnessReport) -> None:
        with self._lock:
            self._views[report.view].freshness_sla = report.sla_met_fraction

    # -- read ------------------------------------------------------------ #

    def snapshot(self) -> MonitorSnapshot:
        with self._lock:
            return MonitorSnapshot(
                counters=dict(self._counters),
                view_health={k: _copy_health(v) for k, v in self._views.items()},
            )

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._views.clear()


def _copy_health(h: ViewHealth) -> ViewHealth:
    return ViewHealth(
        parity_match_rate=h.parity_match_rate,
        skew_drifted=h.skew_drifted,
        freshness_sla=h.freshness_sla,
        rows_materialized=h.rows_materialized,
        last_materialized_coverage=h.last_materialized_coverage,
    )


__all__ = ["FeatureMonitor", "MonitorSnapshot", "ViewHealth"]
