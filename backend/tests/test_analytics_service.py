"""Unit tests for the AnalyticsService façade over the in-memory store (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.analytics.events import EventName, RawEvent
from app.analytics.service import AnalyticsService
from app.analytics.store import InMemoryAnalyticsStore
from app.analytics.timebucket import Granularity

SALT = "svc-salt"
BASE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def raw(
    eid: str,
    name: EventName,
    *,
    minute: float = 0,
    user: str = "alice",
    book: str | None = None,
    props: dict | None = None,
) -> RawEvent:
    return RawEvent(
        event_id=eid,
        name=name.value,
        occurred_at=BASE + timedelta(minutes=minute),
        user_ref=user,
        book_id=book,
        props=props or {},
    )


def _service() -> AnalyticsService:
    return AnalyticsService(InMemoryAnalyticsStore(), salt=SALT, max_batch=10)


async def test_ingest_scrubs_and_dedupes() -> None:
    svc = _service()
    result = await svc.ingest([raw("e1", EventName.APP_OPENED), raw("e2", EventName.PAGE_VIEWED)])
    assert result.received == 2
    assert result.accepted == 2
    assert result.new == 2
    # re-ingest one duplicate + one new
    result2 = await svc.ingest([raw("e1", EventName.APP_OPENED), raw("e3", EventName.SEEK)])
    assert result2.new == 1


async def test_ingest_rejects_oversized_batch() -> None:
    svc = AnalyticsService(InMemoryAnalyticsStore(), salt=SALT, max_batch=1)
    result = await svc.ingest([raw("e1", EventName.APP_OPENED), raw("e2", EventName.SEEK)])
    assert result.rejected == 2
    assert result.new == 0
    assert result.errors


async def test_ingest_pseudonymises_user() -> None:
    svc = _service()
    await svc.ingest([raw("e1", EventName.APP_OPENED, user="alice@example.com")])
    events = await svc.store.query()
    assert events[0].anon_user_id is not None
    assert "alice" not in str(events[0].anon_user_id)


async def test_run_query_through_service() -> None:
    svc = _service()
    await svc.ingest(
        [
            raw("e1", EventName.PAGE_VIEWED, minute=0),
            raw("e2", EventName.PAGE_VIEWED, minute=1),
        ]
    )
    from app.analytics.query import Query, parse_metric

    query = Query(
        metric=parse_metric("count"),
        since=BASE - timedelta(hours=1),
        until=BASE + timedelta(hours=1),
        granularity=Granularity.HOUR,
    )
    result = await svc.run(query)
    assert sum(p.value for s in result.series for p in s.points) == 2.0


async def test_engagement_through_service() -> None:
    svc = _service()
    pv = EventName.PAGE_VIEWED
    await svc.ingest(
        [
            raw("e1", pv, minute=0, book="b1", props={"page": 0, "page_count": 4}),
            raw("e2", pv, minute=5, book="b1", props={"page": 3, "page_count": 4}),
        ]
    )
    summary = await svc.engagement(
        since=BASE - timedelta(hours=1), until=BASE + timedelta(hours=1)
    )
    assert summary.session_count == 1
    assert summary.completion_rate == 1.0


async def test_funnel_through_service() -> None:
    svc = _service()
    await svc.ingest(
        [
            raw("e1", EventName.APP_OPENED, minute=0),
            raw("e2", EventName.BOOK_OPENED, minute=1),
        ]
    )
    result = await svc.funnel(
        [EventName.APP_OPENED, EventName.BOOK_OPENED],
        since=BASE - timedelta(hours=1),
        until=BASE + timedelta(hours=1),
    )
    assert result.total_converted == 1


async def test_retention_through_service() -> None:
    svc = _service()
    await svc.ingest([raw("e1", EventName.APP_OPENED, minute=0)])
    matrix = await svc.retention(
        granularity=Granularity.DAY,
        max_offset=1,
        since=BASE - timedelta(hours=1),
        until=BASE + timedelta(days=2),
    )
    assert matrix.cohorts[0].retained[0] == 1


async def test_compute_rollups_through_service() -> None:
    svc = _service()
    await svc.ingest(
        [raw("e1", EventName.PAGE_VIEWED, minute=0), raw("e2", EventName.SEEK, minute=1)]
    )
    rows = await svc.compute_rollups(
        since=BASE - timedelta(hours=1),
        until=BASE + timedelta(hours=1),
        granularity=Granularity.HOUR,
    )
    assert any(r.metric == "events" and r.value == 2.0 for r in rows)


async def test_empty_ingest() -> None:
    svc = _service()
    result = await svc.ingest([])
    assert result.received == 0
    assert result.new == 0
