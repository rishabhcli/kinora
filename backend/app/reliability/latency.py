"""Streaming latency digest — percentile/throughput accounting (kinora.md §12.5).

A load run produces tens of thousands of per-request latencies; keeping them all
in a list to sort at the end is wasteful and does not *merge* across the virtual
users that run concurrently. :class:`LatencyDigest` is a small, mergeable,
fixed-memory histogram in the spirit of HdrHistogram: latencies bucket into
log-spaced bins so the relative error of any reported percentile is bounded
(``REL_ERROR``), and two digests from two workers combine by summing bin counts.

Everything is pure and synchronous. The unit tests pin the percentile estimates
against a known distribution and against the exact sorted answer within the
documented relative-error bound, and prove that ``merge`` is associative.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: Target relative error for a reported percentile (bucket growth factor − 1).
#: 2% buckets give sub-percent error in practice while keeping ~350 bins over
#: the 0.01 ms .. 1 h range a render pipeline can span.
REL_ERROR = 0.02
_GROWTH = 1.0 + REL_ERROR
_LOG_GROWTH = math.log(_GROWTH)
#: Values at/below this (ms) collapse into bucket 0 (sub-10µs is noise here).
_MIN_TRACKABLE_MS = 0.01


def _bucket_index(value_ms: float) -> int:
    """Map a latency (ms) to its log-spaced bucket index (monotonic, ``>= 0``)."""
    if value_ms <= _MIN_TRACKABLE_MS:
        return 0
    return 1 + int(math.log(value_ms / _MIN_TRACKABLE_MS) / _LOG_GROWTH)


def _bucket_value(index: int) -> float:
    """The representative (geometric-midpoint-ish) latency for a bucket (ms)."""
    if index <= 0:
        return _MIN_TRACKABLE_MS
    low = _MIN_TRACKABLE_MS * (_GROWTH ** (index - 1))
    high = _MIN_TRACKABLE_MS * (_GROWTH**index)
    return math.sqrt(low * high)


@dataclass(frozen=True, slots=True)
class LatencySummary:
    """A flattened, serializable view of a :class:`LatencyDigest`."""

    count: int
    min_ms: float
    max_ms: float
    mean_ms: float
    p50_ms: float
    p90_ms: float
    p95_ms: float
    p99_ms: float
    p999_ms: float

    def to_dict(self) -> dict[str, float | int]:
        """JSON-friendly projection (rounded to 3 decimals)."""
        return {
            "count": self.count,
            "min_ms": round(self.min_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "mean_ms": round(self.mean_ms, 3),
            "p50_ms": round(self.p50_ms, 3),
            "p90_ms": round(self.p90_ms, 3),
            "p95_ms": round(self.p95_ms, 3),
            "p99_ms": round(self.p99_ms, 3),
            "p999_ms": round(self.p999_ms, 3),
        }


class LatencyDigest:
    """A mergeable, fixed-error histogram of request latencies in milliseconds.

    Insertion is O(1); percentile queries are O(buckets); merging two digests is
    O(buckets). The number of buckets is bounded by the log range, so memory is
    independent of the number of samples — a 10-hour run costs the same as a
    10-second one.
    """

    __slots__ = ("_counts", "_total", "_sum", "_min", "_max")

    def __init__(self) -> None:
        self._counts: dict[int, int] = {}
        self._total = 0
        self._sum = 0.0
        self._min = math.inf
        self._max = 0.0

    # -- ingestion ----------------------------------------------------------- #

    def record_ms(self, value_ms: float) -> None:
        """Record one latency sample (milliseconds). Negatives clamp to 0."""
        v = max(0.0, float(value_ms))
        idx = _bucket_index(v)
        self._counts[idx] = self._counts.get(idx, 0) + 1
        self._total += 1
        self._sum += v
        if v < self._min:
            self._min = v
        if v > self._max:
            self._max = v

    def record_s(self, value_s: float) -> None:
        """Record one latency sample given in *seconds*."""
        self.record_ms(value_s * 1000.0)

    def extend_ms(self, values_ms: Iterable[float]) -> None:
        """Record many latency samples (milliseconds)."""
        for v in values_ms:
            self.record_ms(v)

    # -- queries ------------------------------------------------------------- #

    @property
    def count(self) -> int:
        """Number of recorded samples."""
        return self._total

    @property
    def min_ms(self) -> float:
        """Smallest observed latency (ms), or ``0.0`` when empty."""
        return 0.0 if self._total == 0 else self._min

    @property
    def max_ms(self) -> float:
        """Largest observed latency (ms)."""
        return self._max

    @property
    def mean_ms(self) -> float:
        """Arithmetic mean latency (ms), or ``0.0`` when empty."""
        return 0.0 if self._total == 0 else self._sum / self._total

    def quantile_ms(self, q: float) -> float:
        """Estimate the ``q``-quantile latency (ms); ``q`` in ``[0, 1]``.

        Uses the standard HdrHistogram convention: walk the cumulative count to
        the bucket containing the ``ceil(q * N)``-th sample and return that
        bucket's representative value. The relative error is bounded by
        :data:`REL_ERROR`.
        """
        if not 0.0 <= q <= 1.0:
            raise ValueError("quantile q must be in [0, 1]")
        if self._total == 0:
            return 0.0
        # The rank of the requested sample (1-based), at least 1.
        rank = max(1, math.ceil(q * self._total))
        cumulative = 0
        for idx in sorted(self._counts):
            cumulative += self._counts[idx]
            if cumulative >= rank:
                return _bucket_value(idx)
        return self._max

    def summary(self) -> LatencySummary:
        """A flattened percentile/throughput view of this digest."""
        return LatencySummary(
            count=self._total,
            min_ms=self.min_ms,
            max_ms=self._max,
            mean_ms=self.mean_ms,
            p50_ms=self.quantile_ms(0.50),
            p90_ms=self.quantile_ms(0.90),
            p95_ms=self.quantile_ms(0.95),
            p99_ms=self.quantile_ms(0.99),
            p999_ms=self.quantile_ms(0.999),
        )

    # -- composition --------------------------------------------------------- #

    def merge(self, other: LatencyDigest) -> LatencyDigest:
        """Return a new digest combining ``self`` and ``other`` (associative)."""
        out = LatencyDigest()
        out._counts = dict(self._counts)
        for idx, n in other._counts.items():
            out._counts[idx] = out._counts.get(idx, 0) + n
        out._total = self._total + other._total
        out._sum = self._sum + other._sum
        if out._total > 0:
            out._min = min(
                self._min if self._total else math.inf,
                other._min if other._total else math.inf,
            )
            out._max = max(self._max, other._max)
        return out

    def merge_in_place(self, other: LatencyDigest) -> None:
        """Fold ``other`` into ``self`` (cheaper than :meth:`merge` in a loop)."""
        for idx, n in other._counts.items():
            self._counts[idx] = self._counts.get(idx, 0) + n
        self._total += other._total
        self._sum += other._sum
        if other._total:
            self._min = min(self._min, other._min)
            self._max = max(self._max, other._max)

    @classmethod
    def from_samples_ms(cls, samples: Sequence[float]) -> LatencyDigest:
        """Build a digest from an iterable of latency samples (milliseconds)."""
        digest = cls()
        digest.extend_ms(samples)
        return digest


def merge_digests(digests: Iterable[LatencyDigest]) -> LatencyDigest:
    """Reduce many per-worker digests into one (the load-runner aggregation)."""
    out = LatencyDigest()
    for digest in digests:
        out.merge_in_place(digest)
    return out


__all__ = [
    "REL_ERROR",
    "LatencyDigest",
    "LatencySummary",
    "merge_digests",
]
