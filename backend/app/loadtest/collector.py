"""Latency / throughput collection with coordinated-omission correction (§12.5).

**Coordinated omission** is the single most common way load tools lie. Picture an
open-loop run that intends to send a request every 10 ms. The server stalls for
1 s. A naive tool — which sends the *next* request only after the previous one
returns — issues *one* slow 1 s sample and then resumes; the ~100 requests that
*should* have been sent during the stall are simply never recorded. The reported
p99 looks fine even though every real user who arrived during that second waited.

The correction is to measure each request's latency from its **intended send
time**, not the time it was actually dispatched. If a request was supposed to go
out at ``t_intended`` but the sender was busy until ``t_sent``, the user-perceived
latency is ``(t_sent − t_intended) + service_latency`` — the queueing delay is
*included*. And for a long stall we additionally **backfill** the requests that
would have been sent during it: each missed slot at intended time ``t_i`` is
recorded with the latency it *would* have observed had it been served at the same
finish instant, i.e. ``response_finish − t_i``. This reconstructs the tail the
naive tool dropped.

:class:`LatencyCollector` records per-endpoint and aggregate latencies into
:class:`~app.loadtest.histogram.LatencyHistogram`\\ s, counts outcomes, tracks the
run window for throughput, and (optionally) backfills omitted samples given the
schedule's expected inter-arrival interval. The generator feeds it the *intended*
time, the *finish* time, and the response; the collector does the omission math.

Pure and synchronous — the unit tests inject a known stall and assert the
corrected p99 reflects the queueing the naive number hides.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from app.loadtest.histogram import LatencyHistogram, LatencySummary
from app.loadtest.target import LoadResponse, Outcome


@dataclass(slots=True)
class OutcomeCounts:
    """Tally of request outcomes for one endpoint (or the aggregate)."""

    ok: int = 0
    error: int = 0
    timeout: int = 0
    dropped: int = 0

    @property
    def total(self) -> int:
        return self.ok + self.error + self.timeout + self.dropped

    @property
    def errors(self) -> int:
        """Everything that is not a clean success (error + timeout + dropped)."""
        return self.error + self.timeout + self.dropped

    @property
    def error_rate(self) -> float:
        return self.errors / self.total if self.total else 0.0

    def record(self, outcome: Outcome) -> None:
        if outcome is Outcome.OK:
            self.ok += 1
        elif outcome is Outcome.ERROR:
            self.error += 1
        elif outcome is Outcome.TIMEOUT:
            self.timeout += 1
        else:
            self.dropped += 1

    def merge_in(self, other: OutcomeCounts) -> None:
        self.ok += other.ok
        self.error += other.error
        self.timeout += other.timeout
        self.dropped += other.dropped

    def as_dict(self) -> dict[str, int | float]:
        return {
            "ok": self.ok,
            "error": self.error,
            "timeout": self.timeout,
            "dropped": self.dropped,
            "total": self.total,
            "error_rate": self.error_rate,
        }


@dataclass(slots=True)
class EndpointStats:
    """All collected stats for a single endpoint."""

    endpoint: str
    #: Service latency as reported by the target (no omission correction).
    service: LatencyHistogram = field(default_factory=LatencyHistogram)
    #: User-perceived latency from the intended send time (omission-corrected).
    corrected: LatencyHistogram = field(default_factory=LatencyHistogram)
    counts: OutcomeCounts = field(default_factory=OutcomeCounts)

    def merge_in(self, other: EndpointStats) -> None:
        self.service.merge_in(other.service)
        self.corrected.merge_in(other.corrected)
        self.counts.merge_in(other.counts)


class LatencyCollector:
    """Accumulates per-endpoint + aggregate latency/throughput with CO correction.

    ``record`` is the single entry point. The generator calls it once per real
    request with the *intended* send time, the *finish* time, and the response.
    When ``correct_omission`` is on and an ``expected_interval_s`` is known for
    the request's slot, a stall longer than one interval triggers backfill of the
    omitted slots, each recorded against the same finish instant.
    """

    __slots__ = ("_by_endpoint", "_start_s", "_end_s", "correct_omission")

    def __init__(self, *, correct_omission: bool = True) -> None:
        self._by_endpoint: dict[str, EndpointStats] = {}
        self._start_s: float | None = None
        self._end_s: float | None = None
        self.correct_omission = correct_omission

    def _stats(self, endpoint: str) -> EndpointStats:
        st = self._by_endpoint.get(endpoint)
        if st is None:
            st = EndpointStats(endpoint=endpoint)
            self._by_endpoint[endpoint] = st
        return st

    def _touch_window(self, t0: float, t1: float) -> None:
        self._start_s = t0 if self._start_s is None else min(self._start_s, t0)
        self._end_s = t1 if self._end_s is None else max(self._end_s, t1)

    def record(
        self,
        response: LoadResponse,
        *,
        intended_s: float,
        finish_s: float,
        expected_interval_s: float | None = None,
    ) -> None:
        """Record one completed request, applying omission correction.

        * ``service`` latency is ``response.latency_s`` verbatim.
        * ``corrected`` latency is ``finish_s − intended_s`` — the queueing delay
          before dispatch is folded in, so a request that waited behind a stall
          shows the wait.
        * If correction is on and ``expected_interval_s`` is given, every slot
          that *should* have been dispatched between ``intended_s`` and the
          actual finish is backfilled with the latency it would have seen had it
          finished at the same instant. These backfilled samples are counted as
          OK (they are synthetic reconstructions of the omitted real demand).
        """
        st = self._stats(response.endpoint)
        st.counts.record(response.outcome)
        st.service.record(max(0.0, response.latency_s))
        self._touch_window(intended_s, finish_s)

        corrected = max(0.0, finish_s - intended_s)
        st.corrected.record(corrected)

        if (
            self.correct_omission
            and response.outcome is Outcome.OK
            and expected_interval_s
            and expected_interval_s > 0.0
        ):
            self._backfill(
                st,
                intended_s=intended_s,
                finish_s=finish_s,
                interval=expected_interval_s,
            )

    @staticmethod
    def _backfill(
        st: EndpointStats, *, intended_s: float, finish_s: float, interval: float
    ) -> None:
        """Reconstruct omitted arrivals that should have fired during a stall.

        Slots at ``intended_s + k*interval`` for ``k = 1, 2, …`` that fall before
        the actual finish were never sent (the sender was blocked). Each would
        have observed latency ``finish_s − (intended_s + k*interval)``. We stop at
        the finish (a slot arriving after the request already returned is a real
        future request, not an omission). A generous cap prevents a pathological
        backfill from exploding the histogram.
        """
        max_backfill = 100_000
        k = 1
        next_t = intended_s + interval
        while next_t < finish_s and k <= max_backfill:
            st.corrected.record(finish_s - next_t)
            # Backfilled demand is real load that was served; count it OK so the
            # throughput/error denominators reflect the true offered load.
            st.counts.ok += 1
            k += 1
            next_t = intended_s + k * interval

    def record_dropped(self, endpoint: str, *, intended_s: float) -> None:
        """Record a request that backpressure dropped before it ran (§12.2)."""
        st = self._stats(endpoint)
        st.counts.dropped += 1
        self._touch_window(intended_s, intended_s)

    # ----- read-out ------------------------------------------------------- #

    @property
    def endpoints(self) -> list[str]:
        return sorted(self._by_endpoint)

    def stats_for(self, endpoint: str) -> EndpointStats:
        return self._by_endpoint[endpoint]

    def aggregate(self) -> EndpointStats:
        """Merge every endpoint into one aggregate :class:`EndpointStats`."""
        agg = EndpointStats(endpoint="__all__")
        for st in self._by_endpoint.values():
            agg.merge_in(st)
        return agg

    @property
    def elapsed_s(self) -> float:
        if self._start_s is None or self._end_s is None:
            return 0.0
        return max(0.0, self._end_s - self._start_s)

    def throughput_rps(self, *, endpoint: str | None = None) -> float:
        """Completed requests per second over the observed run window."""
        elapsed = self.elapsed_s
        if elapsed <= 0:
            return 0.0
        counts = (
            self._by_endpoint[endpoint].counts if endpoint else self.aggregate().counts
        )
        return counts.total / elapsed

    def corrected_summary(self, *, endpoint: str | None = None) -> LatencySummary:
        hist = (
            self._by_endpoint[endpoint].corrected if endpoint else self.aggregate().corrected
        )
        return hist.summary()

    def service_summary(self, *, endpoint: str | None = None) -> LatencySummary:
        hist = (
            self._by_endpoint[endpoint].service if endpoint else self.aggregate().service
        )
        return hist.summary()

    def per_endpoint_corrected(self) -> Mapping[str, LatencySummary]:
        return {ep: st.corrected.summary() for ep, st in self._by_endpoint.items()}

    def per_endpoint_counts(self) -> Mapping[str, OutcomeCounts]:
        return {ep: st.counts for ep, st in self._by_endpoint.items()}
