"""Unit tests for the persisted rollup/sessionize job + sink (no infra)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.analytics.events import EventName, RawEvent
from app.analytics.service import AnalyticsService
from app.analytics.sink import InMemorySummarySink, SummarySink
from app.analytics.store import InMemoryAnalyticsStore
from app.analytics.timebucket import Granularity

SALT = "job-salt"
BASE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def raw(eid: str, name: EventName, *, minute: float = 0, user: str = "u1") -> RawEvent:
    return RawEvent(
        event_id=eid,
        name=name.value,
        occurred_at=BASE + timedelta(minutes=minute),
        user_ref=user,
        book_id="b1",
        props={"page": int(minute), "page_count": 10},
    )


def test_in_memory_sink_satisfies_protocol() -> None:
    assert isinstance(InMemorySummarySink(), SummarySink)


async def test_rollup_job_persists_rollups_and_sessions() -> None:
    svc = AnalyticsService(InMemoryAnalyticsStore(), salt=SALT)
    await svc.ingest(
        [
            raw("e1", EventName.PAGE_VIEWED, minute=0, user="u1"),
            raw("e2", EventName.PAGE_VIEWED, minute=1, user="u2"),
        ]
    )
    sink = InMemorySummarySink()
    result = await svc.run_rollup_job(
        sink,
        since=BASE - timedelta(hours=1),
        until=BASE + timedelta(hours=1),
        granularities=(Granularity.DAY,),
    )
    assert result.events == 2
    assert result.rollup_rows > 0
    assert result.session_rows == 2  # two users -> two sessions
    # active_users rollup row was written for the day bucket
    au = [r for r in sink.rollups.values() if r.metric == "active_users"]
    assert au and au[0].value == 2.0


async def test_rollup_job_is_idempotent() -> None:
    svc = AnalyticsService(InMemoryAnalyticsStore(), salt=SALT)
    await svc.ingest([raw("e1", EventName.PAGE_VIEWED, minute=0)])
    sink = InMemorySummarySink()
    since = BASE - timedelta(hours=1)
    until = BASE + timedelta(hours=1)
    await svc.run_rollup_job(sink, since=since, until=until, granularities=(Granularity.DAY,))
    keys_after_first = set(sink.rollups.keys())
    sessions_after_first = set(sink.sessions.keys())
    await svc.run_rollup_job(sink, since=since, until=until, granularities=(Granularity.DAY,))
    # No new keys: re-running overwrites in place.
    assert set(sink.rollups.keys()) == keys_after_first
    assert set(sink.sessions.keys()) == sessions_after_first


async def test_rollup_job_multi_granularity() -> None:
    svc = AnalyticsService(InMemoryAnalyticsStore(), salt=SALT)
    await svc.ingest([raw("e1", EventName.PAGE_VIEWED, minute=0)])
    sink = InMemorySummarySink()
    await svc.run_rollup_job(
        sink,
        since=BASE - timedelta(hours=1),
        until=BASE + timedelta(hours=1),
        granularities=(Granularity.DAY, Granularity.WEEK),
    )
    grans = {r.granularity for r in sink.rollups.values()}
    assert Granularity.DAY in grans
    assert Granularity.WEEK in grans


async def test_rollup_job_skip_sessions() -> None:
    svc = AnalyticsService(InMemoryAnalyticsStore(), salt=SALT)
    await svc.ingest([raw("e1", EventName.PAGE_VIEWED, minute=0)])
    sink = InMemorySummarySink()
    result = await svc.run_rollup_job(
        sink,
        since=BASE - timedelta(hours=1),
        until=BASE + timedelta(hours=1),
        granularities=(Granularity.DAY,),
        persist_sessions=False,
    )
    assert result.session_rows == 0
    assert not sink.sessions


async def test_rollup_worker_loop_runs_one_tick(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The worker loop ticks once then stops cleanly when the event is set."""
    from app.analytics import rollup_worker

    ticks = {"n": 0}

    class _FakeContainer:
        async def run_analytics_rollup(self) -> dict[str, int]:
            ticks["n"] += 1
            stop.set()  # stop after the first tick
            return {"events": 0, "rollup_rows": 0, "session_rows": 0}

        async def shutdown(self) -> None:
            return None

    stop = asyncio.Event()
    monkeypatch.setattr(rollup_worker, "build_container", lambda _s: _FakeContainer())
    await asyncio.wait_for(rollup_worker.run_rollup_loop(stop), timeout=5.0)
    assert ticks["n"] == 1
