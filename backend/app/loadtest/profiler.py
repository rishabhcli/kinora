"""A lightweight clock-driven profiler — attribute latency to named phases (§12.5).

A run-wide p99 tells you *that* something is slow; a profiler tells you *where*.
:class:`PhaseProfiler` records, per named phase (e.g. ``"auth"``, ``"canon_query"``,
``"render_enqueue"``), an :class:`~app.loadtest.histogram.LatencyHistogram` of how
long that phase took, plus a call count. A target (or any instrumented code path)
wraps each phase in :meth:`PhaseProfiler.span` — an async context manager that
times the phase on the injected clock and folds it into the right histogram. The
report then breaks the latency budget down by phase so a regression in one phase
(say canon query getting slow) is attributable, not just visible.

Because it times on the injected :class:`~app.loadtest.clock.Clock`, a
:class:`VirtualClock` makes phase timings exactly the modelled service times in
tests — deterministic and assertable. Thread-/task-safe for cooperative asyncio
(no shared mutable span state; each ``span`` owns its own start time).

The profiler is mergeable (per-worker → run-wide) and serializes its summary to a
dict for the JSON report. Pure + clock-driven; no real time, no I/O.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from app.loadtest.clock import Clock
from app.loadtest.histogram import LatencyHistogram, LatencySummary


@dataclass(slots=True)
class PhaseStats:
    """Timing histogram + call count for one named phase."""

    phase: str
    hist: LatencyHistogram = field(default_factory=LatencyHistogram)
    calls: int = 0

    def record(self, seconds: float) -> None:
        self.hist.record(max(0.0, seconds))
        self.calls += 1

    def merge_in(self, other: PhaseStats) -> None:
        self.hist.merge_in(other.hist)
        self.calls += other.calls

    def summary(self) -> LatencySummary:
        return self.hist.summary()


class PhaseProfiler:
    """Records per-phase latency, timed on an injected clock."""

    __slots__ = ("_clock", "_phases")

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._phases: dict[str, PhaseStats] = {}

    def _stats(self, phase: str) -> PhaseStats:
        st = self._phases.get(phase)
        if st is None:
            st = PhaseStats(phase=phase)
            self._phases[phase] = st
        return st

    @asynccontextmanager
    async def span(self, phase: str) -> AsyncIterator[None]:
        """Time the wrapped block as ``phase`` (records even if it raises)."""
        start = self._clock.now()
        try:
            yield
        finally:
            self._stats(phase).record(self._clock.now() - start)

    def record(self, phase: str, seconds: float) -> None:
        """Record a pre-measured phase duration directly."""
        self._stats(phase).record(seconds)

    @property
    def phases(self) -> list[str]:
        return sorted(self._phases)

    def stats_for(self, phase: str) -> PhaseStats:
        return self._phases[phase]

    def merge_in(self, other: PhaseProfiler) -> None:
        for phase, st in other._phases.items():
            self._stats(phase).merge_in(st)

    def summary(self) -> Mapping[str, LatencySummary]:
        return {phase: st.summary() for phase, st in self._phases.items()}

    def total_time_by_phase(self) -> Mapping[str, float]:
        """Sum of time spent in each phase (mean × calls) — share-of-latency view."""
        return {
            phase: st.hist.mean * st.calls for phase, st in self._phases.items()
        }

    def as_dict(self) -> dict[str, object]:
        """A JSON-serializable per-phase breakdown (latencies in milliseconds)."""
        out: dict[str, object] = {}
        for phase, st in self._phases.items():
            s = st.summary()
            out[phase] = {
                "calls": st.calls,
                "mean_ms": s.mean * 1000.0,
                "p50_ms": s.p50 * 1000.0,
                "p95_ms": s.p95 * 1000.0,
                "p99_ms": s.p99 * 1000.0,
                "total_ms": st.hist.mean * st.calls * 1000.0,
            }
        return out
