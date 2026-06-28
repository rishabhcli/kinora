"""Unit tests for the time-bucketed query layer (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.analytics.events import EventName, ReadMode, TrackedEvent
from app.analytics.query import (
    Filters,
    Metric,
    Query,
    parse_metric,
    run_query,
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
    mode: ReadMode | None = None,
    props: dict | None = None,
) -> TrackedEvent:
    return TrackedEvent(
        event_id=eid,
        name=name,
        occurred_at=BASE + timedelta(days=day, hours=1),
        anon_user_id=user,
        book_id=book,
        session_key=session,
        mode=mode,
        props=props or {},
    )


def test_parse_metric() -> None:
    assert parse_metric("count").metric is Metric.COUNT
    assert parse_metric("unique_users").metric is Metric.UNIQUE_USERS
    spec = parse_metric("sum:dwell_ms")
    assert spec.metric is Metric.SUM
    assert spec.prop == "dwell_ms"
    assert parse_metric("avg:velocity_wps").label == "avg:velocity_wps"


def test_parse_metric_errors() -> None:
    with pytest.raises(ValueError, match="requires a prop"):
        parse_metric("sum")
    with pytest.raises(ValueError, match="does not take a prop"):
        parse_metric("count:foo")


def test_count_dense_axis() -> None:
    events = [ev("a", day=0), ev("b", day=0), ev("c", day=2)]
    query = Query(
        metric=parse_metric("count"),
        since=BASE,
        until=BASE + timedelta(days=3),
        granularity=Granularity.DAY,
    )
    result = run_query(query, events)
    assert result.buckets == ["2026-06-01", "2026-06-02", "2026-06-03"]
    series = result.series[0]
    # day0=2, day1=0 (gap-filled), day2=1
    assert [p.value for p in series.points] == [2.0, 0.0, 1.0]
    assert series.total == 3.0


def test_unique_users() -> None:
    events = [
        ev("a", day=0, user="u1"),
        ev("b", day=0, user="u1"),
        ev("c", day=0, user="u2"),
    ]
    query = Query(
        metric=parse_metric("unique_users"),
        since=BASE,
        until=BASE + timedelta(days=1),
    )
    result = run_query(query, events)
    assert result.series[0].points[0].value == 2.0


def test_sum_and_avg_prop() -> None:
    events = [
        ev("a", day=0, props={"clip_seconds": 5}),
        ev("b", day=0, props={"clip_seconds": 15}),
    ]
    end = BASE + timedelta(days=1)
    q_sum = Query(metric=parse_metric("sum:clip_seconds"), since=BASE, until=end)
    q_avg = Query(metric=parse_metric("avg:clip_seconds"), since=BASE, until=end)
    assert run_query(q_sum, events).series[0].total == 20.0
    assert run_query(q_avg, events).series[0].points[0].value == 10.0


def test_group_by_name() -> None:
    events = [
        ev("a", day=0, name=EventName.PAGE_VIEWED),
        ev("b", day=0, name=EventName.SEEK),
        ev("c", day=0, name=EventName.PAGE_VIEWED),
    ]
    query = Query(
        metric=parse_metric("count"),
        since=BASE,
        until=BASE + timedelta(days=1),
        group_by="name",
    )
    result = run_query(query, events)
    groups = {s.group: s.total for s in result.series}
    assert groups[EventName.PAGE_VIEWED.value] == 2.0
    assert groups[EventName.SEEK.value] == 1.0
    # sorted by total desc -> page.viewed first
    assert result.series[0].group == EventName.PAGE_VIEWED.value


def test_group_by_prop() -> None:
    events = [
        ev("a", day=0, props={"source": "upload"}),
        ev("b", day=0, props={"source": "public_domain"}),
        ev("c", day=0, props={}),  # no source -> null group
    ]
    query = Query(
        metric=parse_metric("count"),
        since=BASE,
        until=BASE + timedelta(days=1),
        group_by="prop:source",
    )
    result = run_query(query, events)
    groups = {s.group for s in result.series}
    assert "upload" in groups
    assert "public_domain" in groups


def test_filters_narrow_input() -> None:
    events = [
        ev("a", day=0, book="b1", name=EventName.PAGE_VIEWED),
        ev("b", day=0, book="b2", name=EventName.PAGE_VIEWED),
        ev("c", day=0, book="b1", name=EventName.SEEK),
    ]
    query = Query(
        metric=parse_metric("count"),
        since=BASE,
        until=BASE + timedelta(days=1),
        filters=Filters(book_id="b1", names=(EventName.PAGE_VIEWED,)),
    )
    result = run_query(query, events)
    assert result.series[0].total == 1.0


def test_prop_equals_filter() -> None:
    events = [
        ev("a", day=0, props={"platform": "macos"}),
        ev("b", day=0, props={"platform": "web"}),
    ]
    query = Query(
        metric=parse_metric("count"),
        since=BASE,
        until=BASE + timedelta(days=1),
        filters=Filters(prop_equals={"platform": "macos"}),
    )
    assert run_query(query, events).series[0].total == 1.0


def test_top_n() -> None:
    events = [ev(f"u{i}", day=0, props={"feature": f"f{i % 3}"}) for i in range(9)]
    query = Query(
        metric=parse_metric("count"),
        since=BASE,
        until=BASE + timedelta(days=1),
        group_by="prop:feature",
        top_n=2,
    )
    result = run_query(query, events)
    assert len(result.series) == 2


def test_events_outside_window_excluded() -> None:
    events = [ev("a", day=0), ev("b", day=10)]
    query = Query(
        metric=parse_metric("count"),
        since=BASE,
        until=BASE + timedelta(days=2),
    )
    result = run_query(query, events)
    assert result.series[0].total == 1.0
