"""Unit tests for the event-store seam: the :class:`StreamId`, the optimistic
in-memory store, and its concurrency contract. Pure."""

from __future__ import annotations

import pytest

from app.eventsourcing.domain.identifiers import StreamCategory, StreamId
from app.eventsourcing.store.memory import InMemoryEventStore
from app.eventsourcing.store.protocol import (
    ConcurrencyError,
    EventStore,
)


def test_stream_id_value_and_parse() -> None:
    sid = StreamId.session("abc")
    assert sid.value == "session-abc"
    assert StreamId.parse("session-abc") == sid
    assert StreamId.render_shot("s1").value == "rendershot-s1"
    assert StreamId.canon("e1").value == "canon-e1"


def test_stream_id_parse_round_trip_all_categories() -> None:
    for cat in StreamCategory:
        sid = StreamId(cat, "id-with-dashes")
        assert StreamId.parse(sid.value) == sid


def test_stream_id_parse_rejects_unknown_category() -> None:
    with pytest.raises(ValueError, match="unknown stream category"):
        StreamId.parse("bogus-123")


def test_stream_id_parse_rejects_malformed() -> None:
    with pytest.raises(ValueError, match="not a valid stream id"):
        StreamId.parse("nodashes")


def test_stream_id_requires_aggregate_id() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        StreamId(StreamCategory.SESSION, "")


def test_memory_store_is_an_event_store() -> None:
    assert isinstance(InMemoryEventStore(), EventStore)


async def test_append_and_load() -> None:
    store = InMemoryEventStore()
    envs = [
        {"type": "A", "version": 1, "data": {"n": 1}, "meta": {}},
        {"type": "B", "version": 1, "data": {"n": 2}, "meta": {}},
    ]
    result = await store.append("s-1", envs, expected_version=0)
    assert result.first_version == 1
    assert result.last_version == 2
    assert await store.current_version("s-1") == 2

    loaded = await store.load("s-1")
    assert [e.event_type for e in loaded] == ["A", "B"]
    assert [e.version for e in loaded] == [1, 2]
    assert loaded[0].payload == {"n": 1}


async def test_append_conflict_when_expected_version_stale() -> None:
    store = InMemoryEventStore()
    await store.append(
        "s", [{"type": "A", "version": 1, "data": {}, "meta": {}}], expected_version=0
    )
    with pytest.raises(ConcurrencyError) as ei:
        await store.append(
            "s", [{"type": "B", "version": 1, "data": {}, "meta": {}}], expected_version=0
        )
    assert ei.value.expected == 0
    assert ei.value.actual == 1


async def test_append_conflict_leaves_stream_untouched() -> None:
    store = InMemoryEventStore()
    await store.append(
        "s", [{"type": "A", "version": 1, "data": {}, "meta": {}}], expected_version=0
    )
    with pytest.raises(ConcurrencyError):
        await store.append(
            "s", [{"type": "B", "version": 1, "data": {}, "meta": {}}], expected_version=99
        )
    assert await store.current_version("s") == 1
    assert len(await store.load("s")) == 1


async def test_append_none_skips_concurrency_check() -> None:
    store = InMemoryEventStore()
    await store.append(
        "s", [{"type": "A", "version": 1, "data": {}, "meta": {}}], expected_version=0
    )
    # expected_version=None appends regardless of current version.
    result = await store.append(
        "s", [{"type": "B", "version": 1, "data": {}, "meta": {}}], expected_version=None
    )
    assert result.last_version == 2


async def test_load_from_version_filters() -> None:
    store = InMemoryEventStore()
    await store.append(
        "s",
        [
            {"type": "A", "version": 1, "data": {}, "meta": {}},
            {"type": "B", "version": 1, "data": {}, "meta": {}},
            {"type": "C", "version": 1, "data": {}, "meta": {}},
        ],
        expected_version=0,
    )
    loaded = await store.load("s", from_version=1)
    assert [e.event_type for e in loaded] == ["B", "C"]


async def test_load_unknown_stream_is_empty() -> None:
    store = InMemoryEventStore()
    assert await store.load("missing") == []
    assert await store.current_version("missing") == 0


async def test_global_position_is_monotonic_across_streams() -> None:
    store = InMemoryEventStore()
    await store.append(
        "s1", [{"type": "A", "version": 1, "data": {}, "meta": {}}], expected_version=0
    )
    await store.append(
        "s2", [{"type": "B", "version": 1, "data": {}, "meta": {}}], expected_version=0
    )
    positions = [e.global_position for e in store.all_events()]
    assert positions == [1, 2]
