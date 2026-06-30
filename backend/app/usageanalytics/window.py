"""Time-bucketing, tumbling/sliding windows, downsampling & retention tiers.

Pure time math the store and the aggregation engine read. No I/O, never raises on
ordinary input.

* :class:`Granularity` — the bucket sizes a series can be reported at (minute →
  hour → day → week → month) plus the floor/step math that snaps a timestamp to a
  bucket and walks a dense range of bucket starts.
* :func:`tumbling_windows` / :func:`sliding_windows` — generate the
  ``(start, end)`` pairs for non-overlapping (tumbling) and overlapping (sliding)
  windows over a span. Tumbling drives the stored roll-up grid; sliding drives the
  moving-average / anomaly baselines.
* :class:`RetentionPolicy` — the multi-tier retention plan: raw minute data is
  kept for a short window, then **downsampled** to hour, then day, with each tier
  having its own max age. The :func:`downsample_buckets` helper folds a finer grid
  into a coarser one by merging the cells whose starts fall in the same coarse
  bucket — the cell merge math lives in :mod:`app.usageanalytics.events`.
"""

from __future__ import annotations

import enum
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.usageanalytics.events import MetricCell


class Granularity(enum.StrEnum):
    """A time-bucket size. Ordered coarsest-last for downsampling comparisons."""

    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"

    @property
    def rank(self) -> int:
        """A coarseness rank (minute=0 … month=4); higher = coarser."""
        return _GRAN_ORDER.index(self)

    def is_coarser_than(self, other: Granularity) -> bool:
        return self.rank > other.rank

    def floor(self, at: datetime) -> datetime:
        """Snap a timestamp down to the start of its bucket (UTC, tz-aware)."""
        at = _as_utc(at)
        if self is Granularity.MINUTE:
            return at.replace(second=0, microsecond=0)
        if self is Granularity.HOUR:
            return at.replace(minute=0, second=0, microsecond=0)
        if self is Granularity.DAY:
            return at.replace(hour=0, minute=0, second=0, microsecond=0)
        if self is Granularity.WEEK:
            day = at.replace(hour=0, minute=0, second=0, microsecond=0)
            # ISO week: Monday is the first day (weekday() == 0).
            return day - timedelta(days=day.weekday())
        # MONTH
        return at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def next_bucket(self, start: datetime) -> datetime:
        """The start of the bucket after ``start`` (handles month rollover)."""
        start = _as_utc(start)
        if self is Granularity.MINUTE:
            return start + timedelta(minutes=1)
        if self is Granularity.HOUR:
            return start + timedelta(hours=1)
        if self is Granularity.DAY:
            return start + timedelta(days=1)
        if self is Granularity.WEEK:
            return start + timedelta(weeks=1)
        # MONTH — add one calendar month.
        year = start.year + (1 if start.month == 12 else 0)
        month = 1 if start.month == 12 else start.month + 1
        return start.replace(year=year, month=month)

    def buckets(self, since: datetime, until: datetime) -> Iterator[datetime]:
        """Yield every bucket *start* in ``[floor(since), until)`` (dense grid)."""
        cur = self.floor(since)
        until = _as_utc(until)
        # Guard against a degenerate range producing an unbounded loop.
        guard = 0
        while cur < until and guard < _MAX_DENSE_BUCKETS:
            yield cur
            cur = self.next_bucket(cur)
            guard += 1


_GRAN_ORDER: tuple[Granularity, ...] = (
    Granularity.MINUTE,
    Granularity.HOUR,
    Granularity.DAY,
    Granularity.WEEK,
    Granularity.MONTH,
)

#: Hard cap on a dense bucket walk so a malformed range can never hang a request.
_MAX_DENSE_BUCKETS = 200_000


def _as_utc(at: datetime) -> datetime:
    if at.tzinfo is None:
        return at.replace(tzinfo=UTC)
    return at.astimezone(UTC)


# --------------------------------------------------------------------------- #
# Tumbling / sliding window generation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Window:
    """A half-open time window ``[start, end)``."""

    start: datetime
    end: datetime

    def contains(self, at: datetime) -> bool:
        at = _as_utc(at)
        return self.start <= at < self.end

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds()


def tumbling_windows(since: datetime, until: datetime, size: timedelta) -> list[Window]:
    """Non-overlapping, back-to-back windows of ``size`` covering ``[since, until)``.

    The last window is clamped to ``until``. An empty/inverted range yields ``[]``.
    """
    since, until = _as_utc(since), _as_utc(until)
    if size.total_seconds() <= 0 or until <= since:
        return []
    out: list[Window] = []
    cur = since
    guard = 0
    while cur < until and guard < _MAX_DENSE_BUCKETS:
        end = min(cur + size, until)
        out.append(Window(cur, end))
        cur = end
        guard += 1
    return out


def sliding_windows(
    since: datetime, until: datetime, size: timedelta, step: timedelta
) -> list[Window]:
    """Overlapping windows of ``size`` advanced by ``step`` over ``[since, until)``.

    Each window's end is clamped to ``until``; a window starts at every ``step``
    until the start reaches ``until``. ``step <= 0`` or an inverted range → ``[]``.
    """
    since, until = _as_utc(since), _as_utc(until)
    if size.total_seconds() <= 0 or step.total_seconds() <= 0 or until <= since:
        return []
    out: list[Window] = []
    cur = since
    guard = 0
    while cur < until and guard < _MAX_DENSE_BUCKETS:
        out.append(Window(cur, min(cur + size, until)))
        cur = cur + step
        guard += 1
    return out


# --------------------------------------------------------------------------- #
# Retention / downsampling tiers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RetentionTier:
    """One tier of the retention plan: a granularity kept for a maximum age."""

    granularity: Granularity
    max_age: timedelta

    def cutoff(self, now: datetime) -> datetime:
        """The oldest timestamp this tier still keeps (``now - max_age``)."""
        return _as_utc(now) - self.max_age


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """A multi-tier downsampling/retention plan.

    Tiers are ordered finest-first. The canonical plan: keep raw **minute** rows
    for ``raw_age``, then roll up to **hour** for ``hour_age``, then **day** for
    ``day_age``; anything older than the coarsest tier's cutoff is dropped.
    Pure description — :func:`downsample_buckets` and the store apply it.
    """

    tiers: tuple[RetentionTier, ...]

    @classmethod
    def default(cls) -> RetentionPolicy:
        return cls(
            tiers=(
                RetentionTier(Granularity.MINUTE, timedelta(hours=6)),
                RetentionTier(Granularity.HOUR, timedelta(days=7)),
                RetentionTier(Granularity.DAY, timedelta(days=400)),
            )
        )

    def tier_for_age(self, age: timedelta) -> RetentionTier | None:
        """The finest tier that still retains data of this ``age`` (or ``None``)."""
        for tier in self.tiers:
            if age <= tier.max_age:
                return tier
        return None

    @property
    def horizon(self) -> timedelta:
        """The oldest age any tier keeps (the coarsest tier's ``max_age``)."""
        if not self.tiers:
            return timedelta(0)
        return max(t.max_age for t in self.tiers)


def downsample_buckets(
    buckets: dict[datetime, MetricCell], target: Granularity
) -> dict[datetime, MetricCell]:
    """Fold a fine bucket grid into a coarser ``target`` grid by merging cells.

    Each source cell is reassigned to ``target.floor(source_start)`` and merged
    into the coarse cell there. Returns a fresh dict (inputs untouched). The keys
    are coarse bucket *starts*; ordering is by time.
    """
    out: dict[datetime, MetricCell] = {}
    for start, cell in sorted(buckets.items()):
        coarse = target.floor(start)
        dst = out.get(coarse)
        if dst is None:
            out[coarse] = cell.copy()
        else:
            dst.merge(cell)
    return out


__all__ = [
    "Granularity",
    "RetentionPolicy",
    "RetentionTier",
    "Window",
    "downsample_buckets",
    "sliding_windows",
    "tumbling_windows",
]
