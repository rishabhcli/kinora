"""An HDR-style log-bucketed latency histogram with bounded-error percentiles.

A load run produces tens or hundreds of thousands of latencies. Keeping them all
to sort at the end is O(n log n) memory-and-time and does not *merge* across the
many virtual users that record concurrently. :class:`LatencyHistogram` is a
small, mergeable, fixed-memory histogram in the spirit of HdrHistogram:

* Latencies bucket into **log-spaced** bins (growth factor ``1 + REL_ERROR``), so
  the relative error of any reported percentile is bounded by ``REL_ERROR``
  regardless of the value's magnitude — 50µs and 50s are tracked with the same
  *relative* precision. This is the property a fixed linear histogram lacks.
* Two histograms **merge** by summing per-bin counts, so per-user (or per-worker)
  histograms combine into a run-wide one with no loss beyond the per-bin error.
* Percentiles are read by walking the cumulative counts to the bin holding the
  requested rank and returning that bin's representative value.

Everything is pure and synchronous; the unit tests pin percentile estimates
against a known distribution and the exact sorted answer within ``REL_ERROR``,
and prove ``merge`` is associative.

Values are stored in **milliseconds** internally (the natural unit for an API
latency report) but the public API accepts and returns seconds at the edges via
the harness; this module is unit-agnostic — ``record`` takes whatever unit you
feed it and percentiles come back in the same unit. The harness feeds seconds.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: Target relative error for any reported percentile (bucket growth − 1). 1%
#: buckets keep sub-percent error while spanning microseconds to hours in a few
#: hundred bins.
REL_ERROR = 0.01
_GROWTH = 1.0 + REL_ERROR
_LOG_GROWTH = math.log(_GROWTH)
#: Values at/below this collapse into bucket 0 (sub-microsecond is noise).
_MIN_TRACKABLE = 1e-6


def _bucket_index(value: float) -> int:
    """Map a value to its log-spaced bucket index (monotonic, ``>= 0``)."""
    if value <= _MIN_TRACKABLE:
        return 0
    return 1 + int(math.log(value / _MIN_TRACKABLE) / _LOG_GROWTH)


def _bucket_value(index: int) -> float:
    """The representative (geometric-midpoint) value for a bucket."""
    if index <= 0:
        return _MIN_TRACKABLE
    low = _MIN_TRACKABLE * (_GROWTH ** (index - 1))
    high = _MIN_TRACKABLE * (_GROWTH**index)
    return math.sqrt(low * high)


@dataclass(frozen=True, slots=True)
class LatencySummary:
    """A flattened, serializable percentile view of a histogram (seconds)."""

    count: int
    min: float
    max: float
    mean: float
    p50: float
    p90: float
    p95: float
    p99: float
    p999: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "count": self.count,
            "min": self.min,
            "max": self.max,
            "mean": self.mean,
            "p50": self.p50,
            "p90": self.p90,
            "p95": self.p95,
            "p99": self.p99,
            "p999": self.p999,
        }


class LatencyHistogram:
    """A mergeable, fixed-relative-error histogram of latency values.

    Memory is bounded by the number of distinct occupied buckets (a dict), which
    for a realistic latency span is a few hundred entries regardless of sample
    count. The exact ``min``/``max``/``sum`` are tracked separately so the report
    can show true extrema (extrema percentiles are the ones a bucket smears most).
    """

    __slots__ = ("_buckets", "count", "_sum", "_min", "_max")

    def __init__(self) -> None:
        self._buckets: dict[int, int] = {}
        self.count = 0
        self._sum = 0.0
        self._min = math.inf
        self._max = -math.inf

    def record(self, value: float, *, weight: int = 1) -> None:
        """Record ``value`` ``weight`` times (clamped to ``>= 0``)."""
        if weight <= 0:
            return
        v = max(0.0, value)
        idx = _bucket_index(v)
        self._buckets[idx] = self._buckets.get(idx, 0) + weight
        self.count += weight
        self._sum += v * weight
        if v < self._min:
            self._min = v
        if v > self._max:
            self._max = v

    def record_all(self, values: Iterable[float]) -> None:
        for v in values:
            self.record(v)

    def merge(self, other: LatencyHistogram) -> LatencyHistogram:
        """Return a new histogram = ``self`` + ``other`` (associative, commutative)."""
        out = LatencyHistogram()
        out._buckets = dict(self._buckets)
        for idx, n in other._buckets.items():
            out._buckets[idx] = out._buckets.get(idx, 0) + n
        out.count = self.count + other.count
        out._sum = self._sum + other._sum
        out._min = min(self._min, other._min)
        out._max = max(self._max, other._max)
        return out

    def merge_in(self, other: LatencyHistogram) -> None:
        """In-place merge of ``other`` into ``self``."""
        for idx, n in other._buckets.items():
            self._buckets[idx] = self._buckets.get(idx, 0) + n
        self.count += other.count
        self._sum += other._sum
        self._min = min(self._min, other._min)
        self._max = max(self._max, other._max)

    @property
    def min(self) -> float:
        return 0.0 if self.count == 0 else self._min

    @property
    def max(self) -> float:
        return 0.0 if self.count == 0 else self._max

    @property
    def mean(self) -> float:
        return 0.0 if self.count == 0 else self._sum / self.count

    def percentile(self, q: float) -> float:
        """The value at the ``q``-th percentile (``0 <= q <= 100``).

        Uses the *nearest-rank* convention: the rank is ``ceil(q/100 * count)``
        (1-based), and we return the representative value of the bucket holding
        that rank. The exact ``min``/``max`` are returned at the extremes so the
        0th/100th percentiles are not bucket-smeared.
        """
        if self.count == 0:
            return 0.0
        if q <= 0:
            return self.min
        if q >= 100:
            return self.max
        rank = math.ceil(q / 100.0 * self.count)
        rank = max(1, min(rank, self.count))
        cumulative = 0
        for idx in sorted(self._buckets):
            cumulative += self._buckets[idx]
            if cumulative >= rank:
                return _bucket_value(idx)
        return self.max  # pragma: no cover - unreachable when count > 0

    def summary(self) -> LatencySummary:
        return LatencySummary(
            count=self.count,
            min=self.min,
            max=self.max,
            mean=self.mean,
            p50=self.percentile(50),
            p90=self.percentile(90),
            p95=self.percentile(95),
            p99=self.percentile(99),
            p999=self.percentile(99.9),
        )

    def to_bins(self) -> dict[int, int]:
        """Serialize occupied bins (index → count) for a JSON report / baseline."""
        return dict(self._buckets)

    @classmethod
    def from_bins(
        cls, bins: dict[int, int], *, total_sum: float, min_v: float, max_v: float
    ) -> LatencyHistogram:
        """Reconstruct a histogram from a serialized bin map (for baselines)."""
        h = cls()
        h._buckets = {int(k): int(v) for k, v in bins.items()}
        h.count = sum(h._buckets.values())
        h._sum = total_sum
        h._min = min_v if h.count else math.inf
        h._max = max_v if h.count else -math.inf
        return h


def exact_percentile(values: Sequence[float], q: float) -> float:
    """Exact nearest-rank percentile of ``values`` (for test oracles only)."""
    if not values:
        return 0.0
    if q <= 0:
        return min(values)
    if q >= 100:
        return max(values)
    ordered = sorted(values)
    rank = math.ceil(q / 100.0 * len(ordered))
    rank = max(1, min(rank, len(ordered)))
    return ordered[rank - 1]
