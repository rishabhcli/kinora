"""Rolling time-window metric streams for the SLI/SLO engine (kinora.md §12.5).

An SLI is computed over a *rolling window* of recent observations — "what was
the render p95 over the last 5 minutes", "what fraction of reads in the last
hour were buffer-underrun-free". Prometheus answers these with ``rate()`` over a
TSDB; this module gives the in-process SLO engine the same primitive without a
TSDB: a bounded, monotonically-pruned ring of timestamped samples.

Two stream flavours, both pruned to a max horizon and queryable over any
sub-window of that horizon:

* :class:`CounterStream` — boolean good/bad events (a read succeeded; a shot
  rendered). Yields a *success ratio* and good/bad/total counts over a window.
* :class:`SampleStream` — numeric observations (render latency ms). Yields
  percentiles / mean / count over a window.

Everything is deterministic and clock-injected (``now`` is passed in, never read
from the wall clock inside the math) so tests drive synthetic streams with exact
timestamps. Thread-safety is **not** required: the SLO engine evaluates inside
the asyncio event loop, single-threaded, like the rest of the metric emitters.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RatioWindow:
    """The good/bad tally + success ratio of a :class:`CounterStream` sub-window."""

    good: int
    bad: int

    @property
    def total(self) -> int:
        """Total events observed in the window."""
        return self.good + self.bad

    @property
    def ratio(self) -> float:
        """Success fraction in ``[0, 1]``; an empty window is vacuously perfect (1.0).

        An empty window has *no observed failures*, so a "fraction good"
        indicator reads 1.0 — the SLO is not in violation simply because no
        traffic arrived. Burn-rate accounting layered on top treats an empty
        window as zero burn for the same reason.
        """
        if self.total == 0:
            return 1.0
        return self.good / self.total

    @property
    def failure_ratio(self) -> float:
        """Failure fraction in ``[0, 1]``; empty window => 0.0."""
        if self.total == 0:
            return 0.0
        return self.bad / self.total

    def to_dict(self) -> dict[str, object]:
        return {
            "good": self.good,
            "bad": self.bad,
            "total": self.total,
            "ratio": round(self.ratio, 6),
        }


@dataclass(frozen=True, slots=True)
class SampleWindow:
    """Percentile / mean summary of a :class:`SampleStream` sub-window."""

    count: int
    mean: float
    p50: float
    p90: float
    p95: float
    p99: float
    minimum: float
    maximum: float

    @property
    def is_empty(self) -> bool:
        return self.count == 0

    def percentile(self, kind: str) -> float:
        """Look a percentile field up by its SLI-kind suffix (``p50``/``p95``…)."""
        return {
            "p50": self.p50,
            "p90": self.p90,
            "p95": self.p95,
            "p99": self.p99,
            "mean": self.mean,
            "min": self.minimum,
            "max": self.maximum,
        }[kind]

    def to_dict(self) -> dict[str, object]:
        return {
            "count": self.count,
            "mean": round(self.mean, 4),
            "p50": round(self.p50, 4),
            "p90": round(self.p90, 4),
            "p95": round(self.p95, 4),
            "p99": round(self.p99, 4),
            "min": round(self.minimum, 4),
            "max": round(self.maximum, 4),
        }


_EMPTY_SAMPLE = SampleWindow(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def _percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile (``q`` in ``[0, 1]``) of an already-sorted list.

    Nearest-rank (rather than linear interpolation) matches the SRE convention
    of "the slowest request in the fastest q-fraction" and is what the existing
    reliability ``LatencySummary`` uses, keeping the two engines comparable.
    """
    if not sorted_values:
        return 0.0
    if q <= 0:
        return sorted_values[0]
    if q >= 1:
        return sorted_values[-1]
    rank = math.ceil(q * len(sorted_values))
    idx = min(max(rank - 1, 0), len(sorted_values) - 1)
    return sorted_values[idx]


@dataclass(slots=True)
class CounterStream:
    """A pruned ring of boolean good/bad events for a "fraction good" SLI.

    ``horizon_s`` is the longest window any consumer will ask for; samples older
    than ``now - horizon_s`` are dropped on each record/query so memory stays
    bounded by the arrival rate over the horizon. Pass the **longest** burn-rate
    window as the horizon so multi-window alerts can all be served.
    """

    horizon_s: float
    _events: deque[tuple[float, bool]] = field(default_factory=deque)

    def record(self, *, good: bool, now: float, weight: int = 1) -> None:
        """Append ``weight`` good/bad events stamped at ``now`` and prune the tail."""
        if weight < 1:
            raise ValueError("weight must be >= 1")
        for _ in range(weight):
            self._events.append((now, good))
        self._prune(now)

    def _prune(self, now: float) -> None:
        cutoff = now - self.horizon_s
        events = self._events
        while events and events[0][0] < cutoff:
            events.popleft()

    def window(self, *, now: float, window_s: float) -> RatioWindow:
        """Tally good/bad events in ``[now - window_s, now]``."""
        self._prune(now)
        cutoff = now - window_s
        good = bad = 0
        for ts, ok in self._events:
            if ts < cutoff:
                continue
            if ok:
                good += 1
            else:
                bad += 1
        return RatioWindow(good=good, bad=bad)

    def __len__(self) -> int:
        return len(self._events)


@dataclass(slots=True)
class SampleStream:
    """A pruned ring of numeric observations for a percentile/mean SLI."""

    horizon_s: float
    _samples: deque[tuple[float, float]] = field(default_factory=deque)

    def record(self, value: float, *, now: float) -> None:
        """Append ``value`` stamped at ``now`` and prune the tail."""
        self._samples.append((now, float(value)))
        self._prune(now)

    def _prune(self, now: float) -> None:
        cutoff = now - self.horizon_s
        samples = self._samples
        while samples and samples[0][0] < cutoff:
            samples.popleft()

    def window(self, *, now: float, window_s: float) -> SampleWindow:
        """Summarise observations in ``[now - window_s, now]``."""
        self._prune(now)
        cutoff = now - window_s
        values = sorted(v for ts, v in self._samples if ts >= cutoff)
        if not values:
            return _EMPTY_SAMPLE
        total = math.fsum(values)
        return SampleWindow(
            count=len(values),
            mean=total / len(values),
            p50=_percentile(values, 0.50),
            p90=_percentile(values, 0.90),
            p95=_percentile(values, 0.95),
            p99=_percentile(values, 0.99),
            minimum=values[0],
            maximum=values[-1],
        )

    def __len__(self) -> int:
        return len(self._samples)


__all__ = [
    "CounterStream",
    "RatioWindow",
    "SampleStream",
    "SampleWindow",
]
