"""Unit tests for reading-engagement aggregation (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.analytics.engagement import COMPLETION_THRESHOLD, summarize_engagement
from app.analytics.events import EventName, TrackedEvent
from app.analytics.sessionize import sessionize

BASE = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)


def ev(eid: str, *, minute: float, user: str, book: str, props: dict) -> TrackedEvent:
    return TrackedEvent(
        event_id=eid,
        name=EventName.PAGE_VIEWED,
        occurred_at=BASE + timedelta(minutes=minute),
        anon_user_id=user,
        book_id=book,
        props=props,
    )


def test_empty_engagement() -> None:
    summary = summarize_engagement([])
    assert summary.session_count == 0
    assert summary.median_pages_per_min is None
    assert summary.completion_rate is None


def test_engagement_population_metrics() -> None:
    events = [
        # u1 reads b1 deeply (completes)
        ev("a", minute=0, user="u1", book="b1", props={"page": 0, "page_count": 10}),
        ev("b", minute=5, user="u1", book="b1", props={"page": 9, "page_count": 10}),
        # u2 reads b1 shallowly (drops off early)
        ev("c", minute=0, user="u2", book="b1", props={"page": 0, "page_count": 10}),
        ev("d", minute=2, user="u2", book="b1", props={"page": 1, "page_count": 10}),
    ]
    sessions = sessionize(events)
    summary = summarize_engagement(sessions)
    assert summary.session_count == 2
    assert summary.unique_readers == 2
    assert summary.unique_books == 1
    assert summary.completion_rate is not None
    # u1 reached page 9/10 == 1.0 >= threshold; u2 reached 2/10 == 0.2
    assert summary.completion_rate == 0.5
    assert summary.total_reading_seconds == (5 + 2) * 60


def test_dropoff_histogram() -> None:
    events = [
        ev("a", minute=0, user="u1", book="b1", props={"page": 0}),
        ev("b", minute=1, user="u1", book="b1", props={"page": 3}),
        ev("c", minute=0, user="u2", book="b1", props={"page": 0}),
        ev("d", minute=1, user="u2", book="b1", props={"page": 3}),
    ]
    summary = summarize_engagement(sessionize(events))
    # both sessions drop off at page 3
    assert summary.dropoff_histogram == {3: 2}


def test_completion_buckets() -> None:
    events = [
        ev("a", minute=0, user="u1", book="b1", props={"page": 0, "page_count": 10}),
        ev("b", minute=1, user="u1", book="b1", props={"page": 9, "page_count": 10}),
        ev("c", minute=0, user="u2", book="b1", props={"page": 0, "page_count": 10}),
        ev("d", minute=1, user="u2", book="b1", props={"page": 1, "page_count": 10}),
    ]
    summary = summarize_engagement(sessionize(events))
    assert summary.completion_buckets.get("90-100%") == 1
    assert summary.completion_buckets.get("0-25%") == 1


def test_completion_threshold_constant() -> None:
    assert COMPLETION_THRESHOLD == 0.9
