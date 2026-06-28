"""Unit tests for pure time-bucketing helpers (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.analytics.timebucket import (
    Granularity,
    bucket_label,
    bucket_range,
    day_index,
    floor_to_bucket,
    next_bucket,
    week_index,
)


def dt(y: int, m: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=UTC)


def test_floor_hour() -> None:
    assert floor_to_bucket(dt(2026, 6, 28, 13, 47), Granularity.HOUR) == dt(2026, 6, 28, 13, 0)


def test_floor_day() -> None:
    assert floor_to_bucket(dt(2026, 6, 28, 13, 47), Granularity.DAY) == dt(2026, 6, 28)


def test_floor_week_monday_start() -> None:
    # 2026-06-28 is a Sunday; ISO week starts Monday 2026-06-22.
    assert floor_to_bucket(dt(2026, 6, 28), Granularity.WEEK) == dt(2026, 6, 22)


def test_floor_month() -> None:
    assert floor_to_bucket(dt(2026, 6, 28, 9), Granularity.MONTH) == dt(2026, 6, 1)


def test_next_bucket_month_december_wrap() -> None:
    assert next_bucket(dt(2026, 12, 1), Granularity.MONTH) == dt(2027, 1, 1)


def test_next_bucket_day_and_week() -> None:
    assert next_bucket(dt(2026, 6, 1), Granularity.DAY) == dt(2026, 6, 2)
    assert next_bucket(dt(2026, 6, 22), Granularity.WEEK) == dt(2026, 6, 29)


def test_bucket_labels() -> None:
    assert bucket_label(dt(2026, 6, 28, 13), Granularity.HOUR) == "2026-06-28T13:00Z"
    assert bucket_label(dt(2026, 6, 28), Granularity.DAY) == "2026-06-28"
    assert bucket_label(dt(2026, 6, 1), Granularity.MONTH) == "2026-06"
    assert bucket_label(dt(2026, 6, 22), Granularity.WEEK).startswith("2026-W")


def test_bucket_range_dense_days() -> None:
    buckets = bucket_range(dt(2026, 6, 1, 5), dt(2026, 6, 4), Granularity.DAY)
    assert buckets == [dt(2026, 6, 1), dt(2026, 6, 2), dt(2026, 6, 3)]


def test_bucket_range_inverted_is_empty() -> None:
    assert bucket_range(dt(2026, 6, 4), dt(2026, 6, 1), Granularity.DAY) == []
    assert bucket_range(dt(2026, 6, 1), dt(2026, 6, 1), Granularity.DAY) == []


def test_day_and_week_index() -> None:
    assert day_index(dt(2026, 6, 5), dt(2026, 6, 1)) == 4
    assert week_index(dt(2026, 6, 29), dt(2026, 6, 22)) == 1
    assert day_index(dt(2026, 6, 1), dt(2026, 6, 1)) == 0
