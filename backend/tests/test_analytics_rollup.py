"""Unit tests for the rollup / aggregation jobs (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.analytics.events import EventName, TrackedEvent
from app.analytics.rollup import (
    DIM_ALL,
    METRIC_ACTIVE_USERS,
    METRIC_EVENTS,
    METRIC_READING_EVENTS,
    compute_rollups,
)
from app.analytics.timebucket import Granularity

BASE = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


def ev(
    eid: str,
    *,
    day: int = 0,
    name: EventName = EventName.PAGE_VIEWED,
    user: str | None = None,
    book: str | None = None,
    session: str | None = None,
) -> TrackedEvent:
    return TrackedEvent(
        event_id=eid,
        name=name,
        occurred_at=BASE + timedelta(days=day, hours=2),
        anon_user_id=user,
        book_id=book,
        session_key=session,
    )


def _by(rows, metric, dim=DIM_ALL):  # type: ignore[no-untyped-def]
    return {r.bucket_label: r.value for r in rows if r.metric == metric and r.dimension_key == dim}


def test_active_users_per_day() -> None:
    events = [
        ev("a", day=0, user="u1"),
        ev("b", day=0, user="u1"),  # same user same day
        ev("c", day=0, user="u2"),
        ev("d", day=1, user="u1"),
    ]
    rows = compute_rollups(events, granularity=Granularity.DAY)
    au = _by(rows, METRIC_ACTIVE_USERS)
    assert au["2026-06-01"] == 2.0
    assert au["2026-06-02"] == 1.0


def test_event_totals_and_reading() -> None:
    events = [
        ev("a", day=0, name=EventName.PAGE_VIEWED, user="u1"),
        ev("b", day=0, name=EventName.APP_OPENED, user="u1"),
    ]
    rows = compute_rollups(events, granularity=Granularity.DAY)
    assert _by(rows, METRIC_EVENTS)["2026-06-01"] == 2.0
    # only page.viewed is a reading event
    assert _by(rows, METRIC_READING_EVENTS)["2026-06-01"] == 1.0


def test_per_event_name_rows() -> None:
    events = [
        ev("a", day=0, name=EventName.SEEK, user="u1"),
        ev("b", day=0, name=EventName.SEEK, user="u2"),
    ]
    rows = compute_rollups(events, granularity=Granularity.DAY)
    seek = [r for r in rows if r.metric == f"event:{EventName.SEEK.value}"]
    assert len(seek) == 1
    assert seek[0].value == 2.0


def test_rollups_are_idempotent_and_stable() -> None:
    events = [ev("a", day=0, user="u1"), ev("b", day=1, user="u2")]
    first = compute_rollups(events, granularity=Granularity.DAY)
    second = compute_rollups(events, granularity=Granularity.DAY)
    assert [r.key for r in first] == [r.key for r in second]
    assert [r.value for r in first] == [r.value for r in second]


def test_rollup_key_shape() -> None:
    events = [ev("a", day=0, user="u1")]
    row = compute_rollups(events, granularity=Granularity.DAY)[0]
    assert row.key == (row.granularity.value, row.bucket_label, row.dimension_key, row.metric)


def test_week_granularity_active_users() -> None:
    events = [ev("a", day=0, user="u1"), ev("b", day=3, user="u2")]
    rows = compute_rollups(events, granularity=Granularity.WEEK)
    au = _by(rows, METRIC_ACTIVE_USERS)
    # both fall in the same ISO week -> 2 weekly-active users in one bucket
    assert len(au) == 1
    assert next(iter(au.values())) == 2.0
