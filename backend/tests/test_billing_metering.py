"""Tests for usage metering (append-only, idempotent, windowed aggregation)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.billing.enums import MeteredAggregation, UsageMeter
from app.billing.metering import (
    UsageEvent,
    UsageLedger,
    reading_minutes_event,
    render_seconds_event,
    summarize,
)

T0 = datetime(2026, 1, 5, tzinfo=UTC)
T1 = datetime(2026, 1, 10, tzinfo=UTC)
T2 = datetime(2026, 2, 1, tzinfo=UTC)
PERIOD_START = datetime(2026, 1, 1, tzinfo=UTC)
PERIOD_END = datetime(2026, 2, 1, tzinfo=UTC)


def test_event_requires_tz_aware() -> None:
    with pytest.raises(ValueError):
        UsageEvent(
            meter=UsageMeter.RENDER_SECONDS,
            quantity=5,
            at=datetime(2026, 1, 5),  # noqa: DTZ001 - intentional naive
        )


def test_record_and_events() -> None:
    ledger = UsageLedger()
    assert ledger.record(UsageEvent(UsageMeter.RENDER_SECONDS, 5.0, T0))
    assert len(ledger.events()) == 1


def test_idempotent_record() -> None:
    ledger = UsageLedger()
    ev = UsageEvent(UsageMeter.RENDER_SECONDS, 5.0, T0, idempotency_key="shot_1")
    assert ledger.record(ev) is True
    # Same key -> no-op, not appended again.
    assert ledger.record(ev) is False
    assert len(ledger.events()) == 1


def test_aggregate_sum_in_window() -> None:
    ledger = UsageLedger()
    ledger.record(UsageEvent(UsageMeter.RENDER_SECONDS, 5.0, T0, subscription_id="sub_1"))
    ledger.record(UsageEvent(UsageMeter.RENDER_SECONDS, 7.0, T1, subscription_id="sub_1"))
    # Outside the window (Feb) — excluded.
    ledger.record(UsageEvent(UsageMeter.RENDER_SECONDS, 100.0, T2, subscription_id="sub_1"))
    result = ledger.aggregate(
        UsageMeter.RENDER_SECONDS,
        MeteredAggregation.SUM,
        subscription_id="sub_1",
        period_start=PERIOD_START,
        period_end=PERIOD_END,
    )
    assert result.quantity == 12.0
    assert result.event_count == 2


def test_aggregate_scoped_by_subscription() -> None:
    ledger = UsageLedger()
    ledger.record(UsageEvent(UsageMeter.RENDER_SECONDS, 5.0, T0, subscription_id="sub_1"))
    ledger.record(UsageEvent(UsageMeter.RENDER_SECONDS, 9.0, T0, subscription_id="sub_2"))
    result = ledger.aggregate(
        UsageMeter.RENDER_SECONDS, MeteredAggregation.SUM, subscription_id="sub_1"
    )
    assert result.quantity == 5.0


def test_aggregate_max_and_last() -> None:
    ledger = UsageLedger()
    for q in (3.0, 9.0, 4.0):
        ledger.record(UsageEvent(UsageMeter.BOOKS_IMPORTED, q, T0))
    assert ledger.aggregate(UsageMeter.BOOKS_IMPORTED, MeteredAggregation.MAX).quantity == 9.0
    assert ledger.aggregate(UsageMeter.BOOKS_IMPORTED, MeteredAggregation.LAST).quantity == 4.0


def test_window_boundaries_half_open() -> None:
    ledger = UsageLedger()
    ledger.record(UsageEvent(UsageMeter.RENDER_SECONDS, 1.0, PERIOD_START))  # included
    ledger.record(UsageEvent(UsageMeter.RENDER_SECONDS, 1.0, PERIOD_END))  # excluded (>= end)
    events = ledger.in_window(period_start=PERIOD_START, period_end=PERIOD_END)
    assert len(events) == 1


def test_render_seconds_event_keyed_on_shot() -> None:
    ledger = UsageLedger()
    ev1 = render_seconds_event(seconds=5.0, at=T0, shot_id="shot_42", subscription_id="sub_1")
    ev2 = render_seconds_event(seconds=5.0, at=T1, shot_id="shot_42", subscription_id="sub_1")
    assert ledger.record(ev1) is True
    assert ledger.record(ev2) is False  # same shot, idempotent
    assert ev1.idempotency_key == "render_seconds:shot_42"


def test_reading_minutes_event() -> None:
    ev = reading_minutes_event(minutes=12.5, at=T0, subscription_id="sub_1")
    assert ev.meter is UsageMeter.READING_MINUTES
    assert ev.quantity == 12.5


def test_summarize_multi_meter() -> None:
    events = [
        UsageEvent(UsageMeter.RENDER_SECONDS, 5.0, T0),
        UsageEvent(UsageMeter.RENDER_SECONDS, 7.0, T1),
        UsageEvent(UsageMeter.READING_MINUTES, 30.0, T0),
        UsageEvent(UsageMeter.READING_MINUTES, 20.0, T1),
    ]
    summary = summarize(events, period_start=PERIOD_START, period_end=PERIOD_END)
    assert summary.quantity(UsageMeter.RENDER_SECONDS) == 12.0
    assert summary.quantity(UsageMeter.READING_MINUTES) == 50.0
    assert summary.quantity(UsageMeter.BOOKS_IMPORTED) == 0.0


def test_summarize_with_aggregation_override() -> None:
    events = [
        UsageEvent(UsageMeter.BOOKS_IMPORTED, 1.0, T0),
        UsageEvent(UsageMeter.BOOKS_IMPORTED, 5.0, T1),
    ]
    summary = summarize(events, aggregations={UsageMeter.BOOKS_IMPORTED: MeteredAggregation.MAX})
    assert summary.quantity(UsageMeter.BOOKS_IMPORTED) == 5.0
