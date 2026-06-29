"""Unit tests for the CDC typed-event contract + clock (no infra)."""

from __future__ import annotations

import pytest

from app.streaming.cdc.clock import FakeClock, SystemClock
from app.streaming.cdc.events import ChangeEvent, LogPosition, Op, key_str


def test_log_position_total_order() -> None:
    assert LogPosition(1, 0) < LogPosition(1, 1) < LogPosition(2, 0)
    assert LogPosition.zero() == LogPosition(0, 0)
    assert LogPosition(5, 3).next_minor() == LogPosition(5, 4)
    # Sorting a shuffled list recovers the log order.
    positions = [LogPosition(2, 0), LogPosition(1, 1), LogPosition(1, 0)]
    assert sorted(positions) == [LogPosition(1, 0), LogPosition(1, 1), LogPosition(2, 0)]


def test_insert_event_projects_key_and_image() -> None:
    ev = ChangeEvent.insert("books", {"id": "b1", "title": "Dune"}, LogPosition(10, 0))
    assert ev.op is Op.INSERT
    assert ev.key == {"id": "b1"}
    assert ev.after == {"id": "b1", "title": "Dune"}
    assert ev.before is None
    assert ev.row == ev.after
    assert ev.is_row_event and not ev.is_delete and not ev.is_snapshot


def test_update_event_carries_before_and_after() -> None:
    ev = ChangeEvent.update(
        "books",
        {"id": "b1", "title": "Dune"},
        {"id": "b1", "title": "Dune Messiah"},
        LogPosition(11, 0),
    )
    assert ev.op is Op.UPDATE
    assert ev.before == {"id": "b1", "title": "Dune"}
    assert ev.after == {"id": "b1", "title": "Dune Messiah"}
    assert ev.row == ev.after


def test_delete_event_row_is_before_image() -> None:
    ev = ChangeEvent.delete("books", {"id": "b1", "title": "Dune"}, LogPosition(12, 0))
    assert ev.is_delete
    assert ev.after is None
    assert ev.row == {"id": "b1", "title": "Dune"}
    tomb = ev.tombstone()
    assert tomb.op is Op.DELETE and tomb.after is None


def test_read_event_is_snapshot() -> None:
    ev = ChangeEvent.read("books", {"id": "b1"}, LogPosition(0, 0))
    assert ev.is_snapshot and ev.is_row_event
    assert ev.op is Op.READ


def test_composite_key_projection() -> None:
    ev = ChangeEvent.insert(
        "entities",
        {"book_id": "bk", "entity_key": "char_elsa", "version": 3},
        LogPosition(1, 0),
        key_columns=("book_id", "entity_key"),
    )
    assert ev.key == {"book_id": "bk", "entity_key": "char_elsa"}


def test_event_roundtrips_through_dict() -> None:
    ev = ChangeEvent.update("books", {"id": "b1"}, {"id": "b1", "title": "x"}, LogPosition(7, 2))
    again = ChangeEvent.from_dict(ev.to_dict())
    assert again == ev


def test_heartbeat_and_schema_are_not_row_events() -> None:
    hb = ChangeEvent.heartbeat(LogPosition(3, 0))
    assert hb.op is Op.HEARTBEAT and not hb.is_row_event
    sc = ChangeEvent.schema(
        "books", {"id": "str", "title": "str"}, LogPosition(4, 0), schema_version=2
    )
    assert sc.op is Op.SCHEMA and not sc.is_row_event
    assert sc.after == {"id": "str", "title": "str"}


def test_key_str_is_order_independent() -> None:
    assert key_str({"a": 1, "b": 2}) == key_str({"b": 2, "a": 1})


def test_fake_clock_is_deterministic() -> None:
    clk = FakeClock(start=100.0)
    assert clk.time() == 100.0
    assert clk.monotonic() == 0.0
    clk.advance(5.0)
    assert clk.time() == 105.0
    assert clk.monotonic() == 5.0
    with pytest.raises(ValueError):
        clk.advance(-1.0)
    with pytest.raises(ValueError):
        clk.set(50.0)


def test_system_clock_monotonic_nondecreasing() -> None:
    clk = SystemClock()
    a = clk.monotonic()
    b = clk.monotonic()
    assert b >= a
