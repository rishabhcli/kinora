"""Unit tests for ordered-step funnel analysis (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.analytics.events import EventName, TrackedEvent
from app.analytics.funnel import analyze_funnel

BASE = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)

STEPS = [
    EventName.APP_OPENED,
    EventName.BOOK_ADDED,
    EventName.BOOK_OPENED,
    EventName.READING_STARTED,
]


def ev(user: str, name: EventName, *, minute: float) -> TrackedEvent:
    return TrackedEvent(
        event_id=f"{user}-{name.value}-{minute}",
        name=name,
        occurred_at=BASE + timedelta(minutes=minute),
        anon_user_id=user,
    )


def test_full_conversion() -> None:
    events = [
        ev("u1", EventName.APP_OPENED, minute=0),
        ev("u1", EventName.BOOK_ADDED, minute=1),
        ev("u1", EventName.BOOK_OPENED, minute=2),
        ev("u1", EventName.READING_STARTED, minute=3),
    ]
    result = analyze_funnel(events, STEPS)
    assert result.total_entered == 1
    assert result.total_converted == 1
    assert result.overall_conversion == 1.0
    assert result.median_time_to_convert_s == 3 * 60


def test_dropoff_at_each_step() -> None:
    # u1 completes; u2 stops after book.added; u3 only opens app.
    events = [
        ev("u1", EventName.APP_OPENED, minute=0),
        ev("u1", EventName.BOOK_ADDED, minute=1),
        ev("u1", EventName.BOOK_OPENED, minute=2),
        ev("u1", EventName.READING_STARTED, minute=3),
        ev("u2", EventName.APP_OPENED, minute=0),
        ev("u2", EventName.BOOK_ADDED, minute=1),
        ev("u3", EventName.APP_OPENED, minute=0),
    ]
    result = analyze_funnel(events, STEPS)
    assert result.total_entered == 3
    assert result.step(EventName.APP_OPENED).users == 3  # type: ignore[union-attr]
    assert result.step(EventName.BOOK_ADDED).users == 2  # type: ignore[union-attr]
    assert result.step(EventName.BOOK_OPENED).users == 1  # type: ignore[union-attr]
    assert result.step(EventName.READING_STARTED).users == 1  # type: ignore[union-attr]
    assert result.total_converted == 1
    # drop-off at book.opened step is 1 (2 -> 1)
    assert result.step(EventName.BOOK_OPENED).dropoff_from_prev == 1  # type: ignore[union-attr]


def test_order_matters_out_of_order_does_not_convert() -> None:
    # book.opened happens before book.added -> the ordered walk can't match
    # book.opened after book.added, so user stalls at book.added.
    events = [
        ev("u1", EventName.APP_OPENED, minute=0),
        ev("u1", EventName.BOOK_OPENED, minute=1),
        ev("u1", EventName.BOOK_ADDED, minute=2),
    ]
    result = analyze_funnel(events, STEPS)
    assert result.step(EventName.BOOK_ADDED).users == 1  # type: ignore[union-attr]
    assert result.step(EventName.BOOK_OPENED).users == 0  # type: ignore[union-attr]


def test_conversion_window_excludes_late_steps() -> None:
    events = [
        ev("u1", EventName.APP_OPENED, minute=0),
        ev("u1", EventName.BOOK_ADDED, minute=1),
        ev("u1", EventName.BOOK_OPENED, minute=2),
        # reading.started is 2 hours later -> outside a 60-minute window
        ev("u1", EventName.READING_STARTED, minute=120),
    ]
    result = analyze_funnel(events, STEPS, window=timedelta(minutes=60))
    assert result.step(EventName.READING_STARTED).users == 0  # type: ignore[union-attr]
    assert result.total_converted == 0


def test_anonymous_users_skipped() -> None:
    anon = TrackedEvent(
        event_id="x", name=EventName.APP_OPENED, occurred_at=BASE, anon_user_id=None
    )
    result = analyze_funnel([anon], STEPS)
    assert result.total_entered == 0


def test_empty_steps_raises() -> None:
    with pytest.raises(ValueError, match="at least one step"):
        analyze_funnel([], [])


def test_conversion_ratios() -> None:
    events = [
        ev("u1", EventName.APP_OPENED, minute=0),
        ev("u1", EventName.BOOK_ADDED, minute=1),
        ev("u2", EventName.APP_OPENED, minute=0),
    ]
    result = analyze_funnel(events, [EventName.APP_OPENED, EventName.BOOK_ADDED])
    book_added = result.step(EventName.BOOK_ADDED)
    assert book_added is not None
    assert book_added.conversion_from_start == 0.5
    assert book_added.conversion_from_prev == 0.5
