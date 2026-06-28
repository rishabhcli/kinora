"""Unit tests for cohort retention (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.analytics.events import EventName, TrackedEvent
from app.analytics.retention import retention_matrix
from app.analytics.timebucket import Granularity

# Monday, ISO week start.
BASE = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)


def ev(user: str, *, day: int) -> TrackedEvent:
    return TrackedEvent(
        event_id=f"{user}-{day}",
        name=EventName.APP_OPENED,
        occurred_at=BASE + timedelta(days=day),
        anon_user_id=user,
    )


def test_day0_retention_is_full() -> None:
    events = [ev("u1", day=0), ev("u2", day=0)]
    matrix = retention_matrix(events, granularity=Granularity.DAY, max_offset=3)
    assert len(matrix.cohorts) == 1
    cohort = matrix.cohorts[0]
    assert cohort.size == 2
    assert cohort.retained[0] == 2
    assert cohort.rate(0) == 1.0


def test_classic_day_n_retention() -> None:
    # u1 active day 0 and day 2; u2 active only day 0.
    events = [ev("u1", day=0), ev("u1", day=2), ev("u2", day=0)]
    matrix = retention_matrix(events, granularity=Granularity.DAY, max_offset=3)
    cohort = matrix.cohorts[0]
    assert cohort.retained[0] == 2  # both day 0
    assert cohort.retained[1] == 0  # nobody day 1
    assert cohort.retained[2] == 1  # u1 day 2
    assert cohort.rate(2) == 0.5


def test_rolling_retention_is_monotone() -> None:
    events = [ev("u1", day=0), ev("u1", day=3), ev("u2", day=0)]
    matrix = retention_matrix(events, granularity=Granularity.DAY, max_offset=4, rolling=True)
    cohort = matrix.cohorts[0]
    # rolling: active at offset n or later
    assert cohort.retained[0] == 2
    assert cohort.retained[1] == 1  # only u1 has activity >= day 1
    assert cohort.retained[3] == 1
    assert cohort.retained[4] == 0
    # monotone non-increasing
    rates = [cohort.retained[n] for n in range(5)]
    assert rates == sorted(rates, reverse=True)


def test_multiple_cohorts_by_day() -> None:
    # u1 first seen day 0, u2 first seen day 1 -> two cohorts.
    events = [ev("u1", day=0), ev("u2", day=1)]
    matrix = retention_matrix(events, granularity=Granularity.DAY, max_offset=2)
    assert len(matrix.cohorts) == 2
    assert all(c.size == 1 for c in matrix.cohorts)


def test_average_curve_size_weighted() -> None:
    events = [ev("u1", day=0), ev("u1", day=1), ev("u2", day=0)]
    matrix = retention_matrix(events, granularity=Granularity.DAY, max_offset=2)
    curve = matrix.average_curve()
    assert curve[0] == 1.0
    assert curve[1] == 0.5  # 1 of 2 active at offset 1


def test_week_granularity() -> None:
    events = [ev("u1", day=0), ev("u1", day=7)]  # week 0 and week 1
    matrix = retention_matrix(events, granularity=Granularity.WEEK, max_offset=2)
    cohort = matrix.cohorts[0]
    assert cohort.retained[0] == 1
    assert cohort.retained[1] == 1
