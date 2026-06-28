"""Pure time-bucketing helpers shared by rollups and the query layer.

A *granularity* (hour/day/week/month) plus a timestamp yields a deterministic
**bucket start** (the floor of that timestamp to the granularity, in UTC) and an
ISO label. Buckets are half-open ``[start, next_start)``. Weeks are ISO weeks
(Monday-start); months are calendar months.

All functions are pure and timezone-aware (everything is normalised to UTC
first). No I/O, no settings.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime, timedelta


class Granularity(enum.StrEnum):
    """Time-bucket granularity for rollups and queries."""

    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def floor_to_bucket(value: datetime, granularity: Granularity) -> datetime:
    """Return the UTC start of the bucket ``value`` falls into."""
    value = _to_utc(value)
    if granularity is Granularity.HOUR:
        return value.replace(minute=0, second=0, microsecond=0)
    if granularity is Granularity.DAY:
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity is Granularity.WEEK:
        midnight = value.replace(hour=0, minute=0, second=0, microsecond=0)
        # ISO week: Monday is weekday() == 0.
        return midnight - timedelta(days=midnight.weekday())
    # MONTH
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_bucket(start: datetime, granularity: Granularity) -> datetime:
    """Return the start of the bucket immediately after ``start``.

    ``start`` is assumed already floored (a bucket start). For months this steps
    to the first of the next calendar month (handling the December wrap).
    """
    start = _to_utc(start)
    if granularity is Granularity.HOUR:
        return start + timedelta(hours=1)
    if granularity is Granularity.DAY:
        return start + timedelta(days=1)
    if granularity is Granularity.WEEK:
        return start + timedelta(weeks=1)
    # MONTH
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def bucket_label(start: datetime, granularity: Granularity) -> str:
    """A compact, stable ISO-ish label for a bucket start."""
    start = _to_utc(start)
    if granularity is Granularity.HOUR:
        return start.strftime("%Y-%m-%dT%H:00Z")
    if granularity is Granularity.DAY:
        return start.strftime("%Y-%m-%d")
    if granularity is Granularity.WEEK:
        iso = start.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return start.strftime("%Y-%m")


def bucket_range(
    start: datetime, end: datetime, granularity: Granularity
) -> list[datetime]:
    """Return every bucket-start in ``[start, end)`` (inclusive of the start bucket).

    ``start`` and ``end`` may be arbitrary timestamps; the first bucket is the
    floor of ``start`` and buckets continue while ``bucket_start < end``. An empty
    or inverted range yields ``[]``.
    """
    start = _to_utc(start)
    end = _to_utc(end)
    if end <= start:
        return []
    cursor = floor_to_bucket(start, granularity)
    buckets: list[datetime] = []
    # Guard against a pathological loop on absurd ranges.
    max_buckets = 1_000_000
    while cursor < end and len(buckets) < max_buckets:
        buckets.append(cursor)
        cursor = next_bucket(cursor, granularity)
    return buckets


def day_index(value: datetime, origin: datetime) -> int:
    """Whole UTC days from ``origin``'s midnight to ``value``'s midnight (>= 0 ok)."""
    a = floor_to_bucket(origin, Granularity.DAY)
    b = floor_to_bucket(value, Granularity.DAY)
    return (b - a).days


def week_index(value: datetime, origin: datetime) -> int:
    """Whole ISO weeks from ``origin``'s week to ``value``'s week."""
    a = floor_to_bucket(origin, Granularity.WEEK)
    b = floor_to_bucket(value, Granularity.WEEK)
    return (b - a).days // 7


__all__ = [
    "Granularity",
    "bucket_label",
    "bucket_range",
    "day_index",
    "floor_to_bucket",
    "next_bucket",
    "week_index",
]
