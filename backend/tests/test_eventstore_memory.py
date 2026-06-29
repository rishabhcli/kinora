"""In-memory-store-specific tests (zero infra).

Covers behaviour that is awkward to exercise through the shared conformance
driver: validation ordering, the partial-overlap re-append guard, snapshot types,
read-stream end markers, and concurrent appends under the store's lock.
"""

from __future__ import annotations

import asyncio

import pytest

from app.eventsourcing.store import (
    ANY,
    NO_EVENTS,
    NO_STREAM,
    AppendError,
    EventData,
    InMemoryEventStore,
    OptimisticConcurrencyError,
    Snapshot,
)
from app.eventsourcing.store.errors import SerializationError


def _ev(t: str, eid: str | None = None, **payload: object) -> EventData:
    if eid is None:
        return EventData(event_type=t, payload=dict(payload))
    return EventData(event_type=t, payload=dict(payload), event_id=eid)


@pytest.mark.asyncio
async def test_partial_reappend_is_an_error() -> None:
    store = InMemoryEventStore()
    a = _ev("a", "id-a")
    await store.append("s", [a], expected_version=NO_STREAM)
    # A batch mixing the already-stored id-a with a fresh event is a bug.
    with pytest.raises(AppendError):
        await store.append("s", [a, _ev("b", "id-b")], expected_version=ANY)


@pytest.mark.asyncio
async def test_validation_failure_does_not_mutate_state() -> None:
    store = InMemoryEventStore()
    bad = EventData(event_type="t", payload={"obj": object()})  # not JSON-safe
    with pytest.raises(SerializationError):
        await store.append("s", [bad], expected_version=NO_STREAM)
    # Nothing was written; the stream stays empty and the next append starts at 0.
    assert await store.stream_version("s") == NO_EVENTS
    recs = await store.append("s", [_ev("ok")], expected_version=NO_STREAM)
    assert recs[0].version == 0
    assert recs[0].global_position == 1


@pytest.mark.asyncio
async def test_read_stream_is_end_marker() -> None:
    store = InMemoryEventStore()
    await store.append("s", [_ev("a"), _ev("b"), _ev("c")], expected_version=NO_STREAM)
    head = await store.read_stream("s", from_version=0, limit=2)
    assert not head.is_end
    assert head.last_version == 1
    tail = await store.read_stream("s", from_version=2, limit=2)
    assert tail.is_end
    assert tail.last_version == 2


@pytest.mark.asyncio
async def test_snapshot_types_are_independent() -> None:
    store = InMemoryEventStore()
    await store.save(Snapshot(stream_id="s", version=1, state={"a": 1}, snapshot_type="alpha"))
    await store.save(Snapshot(stream_id="s", version=2, state={"b": 2}, snapshot_type="beta"))
    alpha = await store.load_latest("s", snapshot_type="alpha")
    beta = await store.load_latest("s", snapshot_type="beta")
    assert alpha is not None and alpha.state == {"a": 1}
    assert beta is not None and beta.state == {"b": 2}
    assert await store.load_latest("s") is None  # default type untouched


@pytest.mark.asyncio
async def test_concurrent_appends_serialise_one_winner() -> None:
    store = InMemoryEventStore()
    await store.append("s", [_ev("seed")], expected_version=NO_STREAM)

    async def writer(tag: str) -> bool:
        try:
            await store.append("s", [_ev(tag)], expected_version=0)
            return True
        except OptimisticConcurrencyError:
            return False

    results = await asyncio.gather(*(writer(f"w{i}") for i in range(8)))
    # Exactly one writer expecting version 0 may win; the rest conflict.
    assert sum(results) == 1
    assert await store.stream_version("s") == 1


@pytest.mark.asyncio
async def test_event_count_and_read_all_limit_validation() -> None:
    store = InMemoryEventStore()
    await store.append("s", [_ev("a"), _ev("b")], expected_version=NO_STREAM)
    assert store.event_count() == 2
    with pytest.raises(ValueError):
        await store.read_all(limit=0)
    with pytest.raises(ValueError):
        await store.read_all(from_position=-1)
    with pytest.raises(ValueError):
        await store.read_stream("s", from_version=-1)
