"""Unit tests for the in-memory analytics store (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.analytics.events import EventName, TrackedEvent
from app.analytics.store import AnalyticsStore, InMemoryAnalyticsStore

BASE = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)


def ev(
    eid: str,
    *,
    minute: int = 0,
    name: EventName = EventName.PAGE_VIEWED,
    book: str | None = None,
    user: str | None = None,
    session: str | None = None,
) -> TrackedEvent:
    return TrackedEvent(
        event_id=eid,
        name=name,
        occurred_at=BASE + timedelta(minutes=minute),
        anon_user_id=user,
        book_id=book,
        session_key=session,
    )


async def test_in_memory_store_satisfies_protocol() -> None:
    store = InMemoryAnalyticsStore()
    assert isinstance(store, AnalyticsStore)


async def test_append_is_idempotent() -> None:
    store = InMemoryAnalyticsStore()
    assert await store.append([ev("a"), ev("b")]) == 2
    # Re-appending the same ids inserts nothing new.
    assert await store.append([ev("a"), ev("b"), ev("c")]) == 1
    assert await store.count() == 3


async def test_append_within_batch_dedup() -> None:
    store = InMemoryAnalyticsStore()
    assert await store.append([ev("a"), ev("a")]) == 1
    assert await store.count() == 1


async def test_query_filters() -> None:
    store = InMemoryAnalyticsStore()
    await store.append(
        [
            ev("a", minute=0, book="b1", user="u1", name=EventName.PAGE_VIEWED),
            ev("b", minute=1, book="b2", user="u2", name=EventName.SEEK),
            ev("c", minute=2, book="b1", user="u1", name=EventName.PAGE_VIEWED),
        ]
    )
    by_book = await store.query(book_id="b1")
    assert {e.event_id for e in by_book} == {"a", "c"}
    by_user = await store.query(anon_user_id="u2")
    assert {e.event_id for e in by_user} == {"b"}
    by_name = await store.query(names=[EventName.SEEK])
    assert {e.event_id for e in by_name} == {"b"}


async def test_query_time_window_half_open() -> None:
    store = InMemoryAnalyticsStore()
    await store.append([ev("a", minute=0), ev("b", minute=10), ev("c", minute=20)])
    rows = await store.query(since=BASE + timedelta(minutes=10), until=BASE + timedelta(minutes=20))
    # since inclusive, until exclusive
    assert [e.event_id for e in rows] == ["b"]


async def test_query_is_sorted_and_stable() -> None:
    store = InMemoryAnalyticsStore()
    await store.append([ev("z", minute=5), ev("a", minute=5), ev("m", minute=1)])
    rows = await store.query()
    # m (minute 1) first, then a, z (same time, event_id tiebreak)
    assert [e.event_id for e in rows] == ["m", "a", "z"]


async def test_query_limit() -> None:
    store = InMemoryAnalyticsStore()
    await store.append([ev(str(i), minute=i) for i in range(10)])
    assert len(await store.query(limit=3)) == 3


async def test_clear() -> None:
    store = InMemoryAnalyticsStore()
    await store.append([ev("a")])
    store.clear()
    assert await store.count() == 0
    # cleared ids can be re-appended
    assert await store.append([ev("a")]) == 1
