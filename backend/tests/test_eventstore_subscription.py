"""Catch-up subscription + checkpoint tests (zero infra).

Drives a :class:`CatchUpSubscription` over the in-memory event store + in-memory
checkpoint store, covering the projection-facet contract: ordered delivery,
durable resume, batch paging, fail-stop with FAILED checkpoint, pause/resume,
reset/rebuild, and exactly-once-when-paired-with-idempotency semantics.
"""

from __future__ import annotations

import pytest

from app.eventsourcing.store import (
    NO_STREAM,
    CatchUpSubscription,
    Checkpoint,
    CheckpointStatus,
    EventData,
    InMemoryCheckpointStore,
    InMemoryEventStore,
    RecordedEvent,
)


def _ev(t: str) -> EventData:
    return EventData(event_type=t, payload={"t": t})


async def _seed(store: InMemoryEventStore, n: int, stream: str = "s") -> None:
    await store.append(
        stream, [_ev(f"e{i}") for i in range(n)], expected_version=NO_STREAM
    )


class Collector:
    def __init__(self) -> None:
        self.seen: list[RecordedEvent] = []

    async def __call__(self, event: RecordedEvent) -> None:
        self.seen.append(event)


@pytest.mark.asyncio
async def test_checkpoint_store_defaults_to_zero() -> None:
    cps = InMemoryCheckpointStore()
    cp = await cps.load("proj")
    assert cp.position == 0
    assert cp.status is CheckpointStatus.ACTIVE
    assert cp.events_processed == 0


@pytest.mark.asyncio
async def test_subscription_processes_all_events_in_order() -> None:
    store = InMemoryEventStore()
    await _seed(store, 5)
    sink = Collector()
    sub = CatchUpSubscription("proj", store, InMemoryCheckpointStore(), sink, batch_size=10)

    result = await sub.run_once()
    assert result.processed == 5
    assert result.caught_up
    assert result.position == 5
    assert [e.global_position for e in sink.seen] == [1, 2, 3, 4, 5]
    assert await sub.position() == 5


@pytest.mark.asyncio
async def test_subscription_resumes_from_checkpoint() -> None:
    store = InMemoryEventStore()
    cps = InMemoryCheckpointStore()
    sink = Collector()
    sub = CatchUpSubscription("proj", store, cps, sink, batch_size=100)

    await _seed(store, 3)
    await sub.run_once()
    assert await sub.position() == 3

    # More events arrive; a second run only delivers the new ones.
    await store.append("s", [_ev("e3"), _ev("e4")], expected_version=2)
    sink.seen.clear()
    result = await sub.run_once()
    assert result.processed == 2
    assert [e.event_type for e in sink.seen] == ["e3", "e4"]
    assert await sub.position() == 5


@pytest.mark.asyncio
async def test_subscription_pages_through_batches() -> None:
    store = InMemoryEventStore()
    await _seed(store, 25)
    sink = Collector()
    sub = CatchUpSubscription("proj", store, InMemoryCheckpointStore(), sink, batch_size=10)

    r1 = await sub.run_once()
    assert r1.processed == 10 and not r1.caught_up
    r2 = await sub.run_once()
    assert r2.processed == 10 and not r2.caught_up
    r3 = await sub.run_once()
    assert r3.processed == 5 and r3.caught_up
    assert len(sink.seen) == 25


@pytest.mark.asyncio
async def test_run_until_caught_up_drains_everything() -> None:
    store = InMemoryEventStore()
    await _seed(store, 47)
    sink = Collector()
    sub = CatchUpSubscription("proj", store, InMemoryCheckpointStore(), sink, batch_size=10)
    result = await sub.run_until_caught_up()
    assert result.processed == 47
    assert result.caught_up
    assert len(sink.seen) == 47


@pytest.mark.asyncio
async def test_empty_log_is_immediately_caught_up() -> None:
    store = InMemoryEventStore()
    sub = CatchUpSubscription("proj", store, InMemoryCheckpointStore(), Collector())
    result = await sub.run_once()
    assert result.processed == 0
    assert result.caught_up


# --------------------------------------------------------------------------- #
# Fail-stop
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_handler_failure_stops_at_last_good_position() -> None:
    store = InMemoryEventStore()
    await _seed(store, 5)
    cps = InMemoryCheckpointStore()
    seen: list[int] = []

    async def handler(event: RecordedEvent) -> None:
        if event.global_position == 3:
            raise RuntimeError("projection blew up on e2")
        seen.append(event.global_position)

    sub = CatchUpSubscription("proj", store, cps, handler, batch_size=10)
    result = await sub.run_once()
    assert result.failed
    assert result.error and "blew up" in result.error
    # Processed events 1,2 (positions 1,2); stopped before 3.
    assert seen == [1, 2]
    assert result.position == 2
    cp = await cps.load("proj")
    assert cp.status is CheckpointStatus.FAILED
    assert cp.position == 2


@pytest.mark.asyncio
async def test_resume_after_failure_continues() -> None:
    store = InMemoryEventStore()
    await _seed(store, 4)
    cps = InMemoryCheckpointStore()
    attempts = {"n": 0}
    seen: list[int] = []

    async def handler(event: RecordedEvent) -> None:
        # Fail the very first time we touch position 2, then succeed on retry.
        if event.global_position == 2 and attempts["n"] == 0:
            attempts["n"] += 1
            raise RuntimeError("transient")
        seen.append(event.global_position)

    sub = CatchUpSubscription("proj", store, cps, handler, batch_size=10)
    first = await sub.run_once()
    assert first.failed
    assert seen == [1]  # only position 1 committed

    await sub.resume()
    second = await sub.run_until_caught_up()
    assert not second.failed
    assert seen == [1, 2, 3, 4]
    assert await sub.position() == 4


# --------------------------------------------------------------------------- #
# Ops controls
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pause_blocks_processing_until_resumed() -> None:
    store = InMemoryEventStore()
    await _seed(store, 3)
    cps = InMemoryCheckpointStore()
    sink = Collector()
    sub = CatchUpSubscription("proj", store, cps, sink, batch_size=10)

    await sub.pause()
    result = await sub.run_once()
    assert result.processed == 0
    assert not sink.seen
    assert (await cps.load("proj")).status is CheckpointStatus.PAUSED

    await sub.resume()
    result2 = await sub.run_once()
    assert result2.processed == 3
    assert len(sink.seen) == 3


@pytest.mark.asyncio
async def test_reset_rewinds_and_reprocesses() -> None:
    store = InMemoryEventStore()
    await _seed(store, 4)
    cps = InMemoryCheckpointStore()
    sink = Collector()
    sub = CatchUpSubscription("proj", store, cps, sink, batch_size=10)

    await sub.run_until_caught_up()
    assert len(sink.seen) == 4

    # Rebuild from scratch.
    await sub.reset()
    assert await sub.position() == 0
    sink.seen.clear()
    await sub.run_until_caught_up()
    assert len(sink.seen) == 4  # reprocessed everything


@pytest.mark.asyncio
async def test_reset_to_position_partial_replay() -> None:
    store = InMemoryEventStore()
    await _seed(store, 6)
    cps = InMemoryCheckpointStore()
    sink = Collector()
    sub = CatchUpSubscription("proj", store, cps, sink, batch_size=10)
    await sub.run_until_caught_up()

    await sub.reset(to_position=3)
    sink.seen.clear()
    await sub.run_until_caught_up()
    assert [e.global_position for e in sink.seen] == [4, 5, 6]


@pytest.mark.asyncio
async def test_batch_size_validation() -> None:
    store = InMemoryEventStore()
    with pytest.raises(ValueError):
        CatchUpSubscription("p", store, InMemoryCheckpointStore(), Collector(), batch_size=0)


@pytest.mark.asyncio
async def test_independent_subscriptions_track_separately() -> None:
    store = InMemoryEventStore()
    await _seed(store, 3)
    cps = InMemoryCheckpointStore()
    a, b = Collector(), Collector()
    sub_a = CatchUpSubscription("a", store, cps, a)
    sub_b = CatchUpSubscription("b", store, cps, b)

    await sub_a.run_until_caught_up()
    assert len(a.seen) == 3
    assert len(b.seen) == 0  # b hasn't run yet — independent checkpoint
    assert await sub_b.position() == 0

    await sub_b.run_until_caught_up()
    assert len(b.seen) == 3


@pytest.mark.asyncio
async def test_checkpoint_value_object_defaults() -> None:
    cp = Checkpoint(subscription="x")
    assert cp.position == 0
    assert cp.status is CheckpointStatus.ACTIVE
    assert cp.last_error is None
