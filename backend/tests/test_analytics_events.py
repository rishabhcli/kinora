"""Unit tests for the analytics event taxonomy + envelopes (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime, timezone

import pytest
from pydantic import ValidationError

from app.analytics.events import (
    READING_EVENTS,
    EventBatch,
    EventName,
    RawEvent,
    ReadMode,
    TrackedEvent,
)


def _now() -> datetime:
    return datetime(2026, 6, 28, 12, 0, tzinfo=UTC)


def test_event_name_is_known() -> None:
    assert EventName.is_known("page.viewed")
    assert EventName.is_known(EventName.SEEK.value)
    assert not EventName.is_known("totally.bogus")
    assert not EventName.is_known("")


def test_raw_event_rejects_unknown_name() -> None:
    with pytest.raises(ValidationError):
        RawEvent(event_id="e1", name="not.a.real.event", occurred_at=_now())


def test_raw_event_accepts_known_name_and_parses_enum() -> None:
    raw = RawEvent(event_id="e1", name="reading.started", occurred_at=_now())
    assert raw.event_name is EventName.READING_STARTED


def test_raw_event_requires_event_id() -> None:
    with pytest.raises(ValidationError):
        RawEvent(event_id="", name="app.opened", occurred_at=_now())


def test_naive_timestamp_coerced_to_utc() -> None:
    naive = datetime(2026, 6, 28, 12, 0)  # noqa: DTZ001 - intentional naive input
    raw = RawEvent(event_id="e1", name="app.opened", occurred_at=naive)
    assert raw.occurred_at.tzinfo is not None
    assert raw.occurred_at == datetime(2026, 6, 28, 12, 0, tzinfo=UTC)


def test_aware_non_utc_timestamp_normalised_to_utc() -> None:
    est = timezone(__import__("datetime").timedelta(hours=-5))
    aware = datetime(2026, 6, 28, 7, 0, tzinfo=est)  # == 12:00 UTC
    raw = RawEvent(event_id="e1", name="app.opened", occurred_at=aware)
    assert raw.occurred_at == datetime(2026, 6, 28, 12, 0, tzinfo=UTC)


def test_mode_coercion() -> None:
    raw = RawEvent(event_id="e1", name="mode.switched", occurred_at=_now(), mode="director")
    assert raw.mode is ReadMode.DIRECTOR


def test_event_batch_default_empty() -> None:
    assert EventBatch().events == []


def test_tracked_event_prop_accessors() -> None:
    event = TrackedEvent(
        event_id="e1",
        name=EventName.PAGE_VIEWED,
        occurred_at=_now(),
        props={"page": "12", "velocity_wps": 4.5, "feature": "karaoke"},
    )
    assert event.prop_int("page") == 12
    assert event.prop_float("velocity_wps") == 4.5
    assert event.prop_str("feature") == "karaoke"
    assert event.prop_int("missing", 99) == 99
    assert event.prop_float("missing") is None
    assert event.prop_str("missing", "x") == "x"


def test_tracked_event_prop_handles_garbage() -> None:
    event = TrackedEvent(
        event_id="e1",
        name=EventName.PAGE_VIEWED,
        occurred_at=_now(),
        props={"page": "not-a-number"},
    )
    assert event.prop_int("page", -1) == -1
    assert event.prop_float("page") is None


def test_reading_events_membership() -> None:
    assert EventName.PAGE_VIEWED in READING_EVENTS
    assert EventName.APP_OPENED not in READING_EVENTS


def test_received_at_defaulted() -> None:
    event = TrackedEvent(event_id="e1", name=EventName.APP_OPENED, occurred_at=_now())
    assert event.received_at.tzinfo is not None
