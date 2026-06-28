"""Unit tests for cohort assignment + per-cohort metrics (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.analytics.cohorts import (
    cohort_by_first_book,
    cohort_by_first_prop,
    cohort_by_platform,
    cohort_by_signup_period,
    cohort_metrics,
    metric_event_count,
    metric_events_per_user,
    metric_reading_event_share,
)
from app.analytics.events import EventName, TrackedEvent
from app.analytics.timebucket import Granularity

BASE = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)  # Monday


def ev(
    eid: str,
    user: str,
    name: EventName,
    *,
    day: int = 0,
    book: str | None = None,
    props: dict | None = None,
) -> TrackedEvent:
    return TrackedEvent(
        event_id=eid,
        name=name,
        occurred_at=BASE + timedelta(days=day),
        anon_user_id=user,
        book_id=book,
        props=props or {},
    )


def test_cohort_by_signup_period_week() -> None:
    events = [
        ev("a", "u1", EventName.APP_OPENED, day=0),
        ev("b", "u2", EventName.APP_OPENED, day=7),  # next week
    ]
    cohorts = cohort_by_signup_period(events, granularity=Granularity.WEEK)
    assert len(cohorts) == 2


def test_cohort_by_acquisition_source() -> None:
    events = [
        ev("a", "u1", EventName.BOOK_ADDED, props={"source": "upload"}),
        ev("b", "u2", EventName.BOOK_ADDED, props={"source": "public_domain"}),
        ev("c", "u3", EventName.BOOK_ADDED, props={"source": "upload"}),
    ]
    cohorts = cohort_by_first_prop(events, trigger=EventName.BOOK_ADDED, prop="source")
    assert cohorts["upload"] == {"u1", "u3"}
    assert cohorts["public_domain"] == {"u2"}


def test_cohort_by_first_prop_uses_earliest() -> None:
    events = [
        ev("a", "u1", EventName.BOOK_ADDED, day=1, props={"source": "public_domain"}),
        ev("b", "u1", EventName.BOOK_ADDED, day=0, props={"source": "upload"}),
    ]
    cohorts = cohort_by_first_prop(events, trigger=EventName.BOOK_ADDED, prop="source")
    # earliest (day 0) is "upload"
    assert "u1" in cohorts["upload"]
    assert "public_domain" not in cohorts


def test_cohort_by_platform() -> None:
    events = [
        ev("a", "u1", EventName.APP_OPENED, props={"platform": "macos"}),
        ev("b", "u2", EventName.APP_OPENED, props={"platform": "web"}),
    ]
    cohorts = cohort_by_platform(events)
    assert cohorts["macos"] == {"u1"}
    assert cohorts["web"] == {"u2"}


def test_cohort_by_first_book() -> None:
    events = [
        ev("a", "u1", EventName.BOOK_OPENED, book="b1"),
        ev("b", "u2", EventName.BOOK_OPENED, book="b2"),
    ]
    cohorts = cohort_by_first_book(events)
    assert cohorts["b1"] == {"u1"}
    assert cohorts["b2"] == {"u2"}


def test_cohort_metrics_sorted_desc() -> None:
    events = [
        ev("a", "u1", EventName.BOOK_ADDED, props={"source": "upload"}),
        ev("c", "u1", EventName.PAGE_VIEWED),
        ev("d", "u1", EventName.PAGE_VIEWED),
        ev("b", "u2", EventName.BOOK_ADDED, props={"source": "public_domain"}),
    ]
    assignment = cohort_by_first_prop(events, trigger=EventName.BOOK_ADDED, prop="source")
    rows = cohort_metrics(events, assignment, metric_event_count)
    # upload cohort (u1) has 3 events; public_domain (u2) has 1
    assert rows[0].label == "upload"
    assert rows[0].value == 3.0
    assert rows[1].value == 1.0


def test_metric_helpers() -> None:
    events = [
        ev("a", "u1", EventName.PAGE_VIEWED),
        ev("b", "u1", EventName.APP_OPENED),
        ev("c", "u2", EventName.PAGE_VIEWED),
    ]
    assert metric_event_count(events) == 3.0
    assert metric_events_per_user(events) == 1.5  # 3 events / 2 users
    # 2 of 3 events are reading touches
    assert abs(metric_reading_event_share(events) - 2 / 3) < 1e-9
    assert metric_reading_event_share([]) == 0.0
