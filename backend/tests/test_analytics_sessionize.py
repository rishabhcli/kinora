"""Unit tests for gap-based sessionization (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.analytics.events import EventName, ReadMode, TrackedEvent
from app.analytics.sessionize import DEFAULT_GAP, sessionize

BASE = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)


def ev(
    eid: str,
    *,
    minute: float = 0,
    user: str = "u1",
    book: str = "b1",
    name: EventName = EventName.PAGE_VIEWED,
    mode: ReadMode | None = None,
    props: dict | None = None,
) -> TrackedEvent:
    return TrackedEvent(
        event_id=eid,
        name=name,
        occurred_at=BASE + timedelta(minutes=minute),
        anon_user_id=user,
        book_id=book,
        mode=mode,
        props=props or {},
    )


def test_single_session_within_gap() -> None:
    events = [ev("a", minute=0), ev("b", minute=5), ev("c", minute=10)]
    sessions = sessionize(events)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.event_count == 3
    assert s.duration_s == 10 * 60


def test_gap_splits_sessions() -> None:
    # 31-minute gap > default 30-minute gap -> two sessions.
    events = [ev("a", minute=0), ev("b", minute=31)]
    sessions = sessionize(events)
    assert len(sessions) == 2


def test_book_change_splits_session() -> None:
    events = [ev("a", minute=0, book="b1"), ev("b", minute=1, book="b2")]
    sessions = sessionize(events)
    assert len(sessions) == 2
    assert {s.book_id for s in sessions} == {"b1", "b2"}


def test_different_users_separate_sessions() -> None:
    events = [ev("a", minute=0, user="u1"), ev("b", minute=1, user="u2")]
    sessions = sessionize(events)
    assert len(sessions) == 2


def test_pages_and_engagement_metrics() -> None:
    events = [
        ev("a", minute=0, props={"page": 0, "page_count": 10, "word_index": 0}),
        ev("b", minute=1, props={"page": 1, "page_count": 10, "word_index": 250}),
        ev("c", minute=2, props={"page": 2, "page_count": 10, "word_index": 500}),
        ev("c2", minute=2, props={"page": 2, "page_count": 10, "word_index": 480}),
    ]
    s = sessionize(events)[0]
    assert s.pages_seen == 3
    assert s.deepest_page == 2
    assert s.dropoff_page == 2
    # completion = (deepest_page + 1) / page_count = 3/10
    assert s.completion_ratio == 0.3
    # words: max word_index per page summed = 0 + 250 + 500 = 750
    assert s.words_read == 750
    assert s.pages_per_min is not None


def test_director_and_stall_counts() -> None:
    events = [
        ev("a", minute=0),
        ev("b", minute=1, name=EventName.DIRECTOR_COMMENT),
        ev("c", minute=2, mode=ReadMode.DIRECTOR),
        ev("d", minute=3, name=EventName.BUFFER_STALL),
    ]
    s = sessionize(events)[0]
    assert s.director_event_count == 2  # the comment + the director-mode event
    assert s.is_director_session
    assert s.stall_count == 1


def test_pages_per_min_none_on_zero_duration() -> None:
    s = sessionize([ev("a", minute=0, props={"page": 1})])[0]
    assert s.duration_s == 0
    assert s.pages_per_min is None


def test_completion_capped_at_one() -> None:
    # deepest_page beyond page_count clamps to 1.0
    events = [ev("a", minute=0, props={"page": 20, "page_count": 10})]
    s = sessionize(events)[0]
    assert s.completion_ratio == 1.0


def test_default_gap_is_30_min() -> None:
    assert timedelta(minutes=30) == DEFAULT_GAP


def test_custom_gap() -> None:
    events = [ev("a", minute=0), ev("b", minute=2)]
    # With a 1-minute gap, the 2-minute spacing splits them.
    sessions = sessionize(events, gap=timedelta(minutes=1))
    assert len(sessions) == 2
