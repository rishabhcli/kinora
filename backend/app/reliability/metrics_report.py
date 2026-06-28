"""Load-run reporting — throughput, errors, and latency percentiles (§12.5/§13).

A load run is a stream of :class:`RequestOutcome` records (one per request the
runner issued). :class:`EndpointStats` accumulates outcomes for one logical
endpoint (``POST /sessions/{id}/intent`` etc.) into a :class:`LatencyDigest`
plus success/error counters; :class:`LoadReport` aggregates per-endpoint stats
across the whole run and exposes the headline numbers a §13 demo panel wants:
requests/second throughput, error rate, and the p50/p90/p99 latency tail.

Pure and synchronous: the async runner feeds outcomes in; the report renders to
a console table and a JSON document. SLA gating lives in :mod:`app.reliability.slo`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.reliability.latency import LatencyDigest, LatencySummary, merge_digests

#: HTTP status (or 0 for a transport-level failure: timeout / connection refused).
StatusCode = int


@dataclass(frozen=True, slots=True)
class RequestOutcome:
    """One completed (or failed) request the runner issued.

    ``ok`` is the caller's success verdict (2xx by default; a scenario may treat
    a 429 as expected backpressure rather than an error). ``error`` is set for a
    transport failure (no HTTP response) with ``status == 0``.
    """

    endpoint: str
    status: StatusCode
    latency_ms: float
    ok: bool
    error: str | None = None

    @property
    def transport_failure(self) -> bool:
        """True for a connection-level failure (timeout/refused), not an HTTP error."""
        return self.status == 0


class EndpointStats:
    """Accumulated outcomes for one logical endpoint."""

    __slots__ = ("endpoint", "_digest", "_ok", "_errors", "_status")

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._digest = LatencyDigest()
        self._ok = 0
        self._errors = 0
        self._status: Counter[StatusCode] = Counter()

    def record(self, outcome: RequestOutcome) -> None:
        """Fold one outcome into this endpoint's stats."""
        self._digest.record_ms(outcome.latency_ms)
        self._status[outcome.status] += 1
        if outcome.ok:
            self._ok += 1
        else:
            self._errors += 1

    @property
    def total(self) -> int:
        """Number of requests recorded for this endpoint."""
        return self._ok + self._errors

    @property
    def ok(self) -> int:
        """Number of successful requests."""
        return self._ok

    @property
    def errors(self) -> int:
        """Number of failed requests (HTTP error or transport failure)."""
        return self._errors

    @property
    def error_rate(self) -> float:
        """Fraction of requests that failed (``0.0`` when none were issued)."""
        return 0.0 if self.total == 0 else self._errors / self.total

    @property
    def digest(self) -> LatencyDigest:
        """The latency digest backing this endpoint's percentiles."""
        return self._digest

    @property
    def status_breakdown(self) -> dict[StatusCode, int]:
        """Per-status-code counts (0 == transport failure)."""
        return dict(self._status)

    def latency(self) -> LatencySummary:
        """The endpoint's latency percentile summary."""
        return self._digest.summary()

    def throughput_rps(self, wall_seconds: float) -> float:
        """Requests/second over a wall-clock window (``0.0`` for a zero window)."""
        return 0.0 if wall_seconds <= 0.0 else self.total / wall_seconds

    def to_dict(self, *, wall_seconds: float) -> dict[str, Any]:
        """JSON projection of this endpoint's stats."""
        return {
            "endpoint": self.endpoint,
            "total": self.total,
            "ok": self._ok,
            "errors": self._errors,
            "error_rate": round(self.error_rate, 6),
            "throughput_rps": round(self.throughput_rps(wall_seconds), 4),
            "status_breakdown": {str(k): v for k, v in sorted(self._status.items())},
            "latency": self.latency().to_dict(),
        }


@dataclass
class LoadReport:
    """The aggregate outcome of a whole load run (§12.5)."""

    #: Wall-clock seconds the run spanned (drives throughput).
    wall_seconds: float = 0.0
    #: Per-endpoint accumulators, keyed by endpoint label.
    endpoints: dict[str, EndpointStats] = field(default_factory=dict)
    #: Free-form run metadata (target, profile, users, seed) for the report header.
    meta: dict[str, Any] = field(default_factory=dict)

    def record(self, outcome: RequestOutcome) -> None:
        """Fold one request outcome into the report."""
        stats = self.endpoints.get(outcome.endpoint)
        if stats is None:
            stats = EndpointStats(outcome.endpoint)
            self.endpoints[outcome.endpoint] = stats
        stats.record(outcome)

    def record_all(self, outcomes: Iterable[RequestOutcome]) -> None:
        """Fold many outcomes (e.g. one worker's batch) into the report."""
        for outcome in outcomes:
            self.record(outcome)

    # -- aggregates ---------------------------------------------------------- #

    @property
    def total_requests(self) -> int:
        """Total requests across every endpoint."""
        return sum(s.total for s in self.endpoints.values())

    @property
    def total_errors(self) -> int:
        """Total failed requests across every endpoint."""
        return sum(s.errors for s in self.endpoints.values())

    @property
    def error_rate(self) -> float:
        """Overall error rate (``0.0`` when nothing was issued)."""
        total = self.total_requests
        return 0.0 if total == 0 else self.total_errors / total

    @property
    def availability(self) -> float:
        """Overall success fraction (``1 - error_rate``)."""
        return 1.0 - self.error_rate

    @property
    def throughput_rps(self) -> float:
        """Aggregate requests/second over the run's wall-clock window."""
        return 0.0 if self.wall_seconds <= 0.0 else self.total_requests / self.wall_seconds

    def overall_latency(self) -> LatencySummary:
        """Latency percentiles across *all* endpoints (merged digests)."""
        return merge_digests(s.digest for s in self.endpoints.values()).summary()

    def to_dict(self) -> dict[str, Any]:
        """A fully serializable view of the report (the ``--out`` JSON)."""
        return {
            "meta": dict(self.meta),
            "wall_seconds": round(self.wall_seconds, 4),
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
            "error_rate": round(self.error_rate, 6),
            "availability": round(self.availability, 6),
            "throughput_rps": round(self.throughput_rps, 4),
            "overall_latency": self.overall_latency().to_dict(),
            "endpoints": [
                self.endpoints[name].to_dict(wall_seconds=self.wall_seconds)
                for name in sorted(self.endpoints)
            ],
        }

    def render_text(self) -> str:
        """A compact, human-readable console report (one line per endpoint)."""
        lines: list[str] = []
        lines.append("=" * 78)
        target = self.meta.get("target", "?")
        profile = self.meta.get("profile", "?")
        lines.append(f"Kinora load report  target={target}  profile={profile}")
        lines.append(
            f"  duration {self.wall_seconds:.1f}s   requests {self.total_requests}   "
            f"throughput {self.throughput_rps:.1f} req/s   "
            f"error-rate {self.error_rate * 100:.2f}%"
        )
        overall = self.overall_latency()
        lines.append(
            f"  latency (ms)  p50 {overall.p50_ms:.1f}  p90 {overall.p90_ms:.1f}  "
            f"p99 {overall.p99_ms:.1f}  max {overall.max_ms:.1f}"
        )
        lines.append("-" * 78)
        header = f"  {'endpoint':<34}{'n':>7}{'err%':>7}{'rps':>8}{'p50':>7}{'p99':>8}"
        lines.append(header)
        for name in sorted(self.endpoints):
            stats = self.endpoints[name]
            summary = stats.latency()
            lines.append(
                f"  {name[:34]:<34}{stats.total:>7}{stats.error_rate * 100:>6.1f}%"
                f"{stats.throughput_rps(self.wall_seconds):>8.1f}"
                f"{summary.p50_ms:>7.0f}{summary.p99_ms:>8.0f}"
            )
        lines.append("=" * 78)
        return "\n".join(lines)


def merge_reports(reports: Sequence[LoadReport]) -> LoadReport:
    """Combine per-worker reports into one (the load-runner fan-in).

    The merged ``wall_seconds`` is the max across reports (the workers ran
    concurrently, so the run spans the longest one). Metadata is taken from the
    first report.
    """
    out = LoadReport()
    if reports:
        out.meta = dict(reports[0].meta)
    out.wall_seconds = max((r.wall_seconds for r in reports), default=0.0)
    for report in reports:
        for stats in report.endpoints.values():
            target = out.endpoints.get(stats.endpoint)
            if target is None:
                target = EndpointStats(stats.endpoint)
                out.endpoints[stats.endpoint] = target
            target._digest.merge_in_place(stats._digest)
            target._ok += stats._ok
            target._errors += stats._errors
            target._status.update(stats._status)
    return out


__all__ = [
    "EndpointStats",
    "LoadReport",
    "RequestOutcome",
    "StatusCode",
    "merge_reports",
]
