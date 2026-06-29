"""Tests for lag tracking, read-your-writes tokens, and blue-green rebuilds."""

from __future__ import annotations

import asyncio

import pytest

from app.eventsourcing.projections.bluegreen import (
    BlueGreenRebuilder,
    InMemorySlotDirectory,
    Slot,
    slot_namespace,
)
from app.eventsourcing.projections.checkpoints import InMemoryCheckpointStore
from app.eventsourcing.projections.lag import (
    ConsistencyToken,
    LagTracker,
    worst_lag,
)
from app.eventsourcing.projections.memory_eventstore import InMemoryEventStore
from app.eventsourcing.projections.projection import Projection, handles
from app.eventsourcing.projections.readmodel import (
    InMemoryReadModelStore,
    ReadModelRow,
)
from app.eventsourcing.projections.runtime import ProjectionRuntime

pytestmark = pytest.mark.asyncio

def _val(row: ReadModelRow | None) -> dict[str, object]:
    assert row is not None
    return row.value



class CountProjection(Projection):
    name = "count"

    @handles("tick")
    async def _on(self, store, ns, ev) -> None:  # type: ignore[no-untyped-def]
        row = await store.get(ns, "n")
        cur = row.value["c"] if row else 0
        await store.put(ns, "n", {"c": cur + 1})


# --------------------------------------------------------------------------- #
# Lag tracking
# --------------------------------------------------------------------------- #


async def test_lag_reflects_uncaught_events() -> None:
    es = InMemoryEventStore()
    cps = InMemoryCheckpointStore()
    for _ in range(5):
        await es.append("s", "tick", {})
    tracker = LagTracker(event_store=es, checkpoints=cps)
    snap = await tracker.snapshot("count")  # never consumed
    assert snap.head_position == 5
    assert snap.checkpoint_position == 0
    assert snap.lag_events == 5
    assert not snap.is_caught_up


async def test_lag_zero_after_catch_up() -> None:
    es = InMemoryEventStore()
    rms = InMemoryReadModelStore()
    cps = InMemoryCheckpointStore()
    for _ in range(3):
        await es.append("s", "tick", {})
    rt = ProjectionRuntime(
        CountProjection(), event_store=es, read_models=rms, checkpoints=cps
    )
    await rt.catch_up()
    tracker = LagTracker(event_store=es, checkpoints=cps)
    snap = await tracker.snapshot("count")
    assert snap.lag_events == 0
    assert snap.is_caught_up


async def test_worst_lag_picks_the_max() -> None:
    es = InMemoryEventStore()
    cps = InMemoryCheckpointStore()
    for _ in range(4):
        await es.append("s", "tick", {})
    await cps.advance("ahead", 4)
    await cps.advance("behind", 1)
    tracker = LagTracker(event_store=es, checkpoints=cps)
    snaps = await tracker.snapshot_all(["ahead", "behind"])
    assert worst_lag(snaps) == 3


# --------------------------------------------------------------------------- #
# Read-your-writes
# --------------------------------------------------------------------------- #


async def test_consistency_token_round_trips() -> None:
    tok = ConsistencyToken.at_head(42, projection="count")
    raw = tok.encode()
    decoded = ConsistencyToken.decode(raw)
    assert decoded.position == 42
    assert decoded.projection == "count"


async def test_has_caught_up_gate() -> None:
    es = InMemoryEventStore()
    cps = InMemoryCheckpointStore()
    await cps.advance("count", 3)
    tracker = LagTracker(event_store=es, checkpoints=cps)
    assert await tracker.has_caught_up(ConsistencyToken(position=3, projection="count"))
    assert not await tracker.has_caught_up(ConsistencyToken(position=4, projection="count"))


async def test_wait_for_returns_when_caught_up() -> None:
    es = InMemoryEventStore()
    rms = InMemoryReadModelStore()
    cps = InMemoryCheckpointStore()
    await es.append("s", "tick", {})
    await es.append("s", "tick", {})
    rt = ProjectionRuntime(
        CountProjection(), event_store=es, read_models=rms, checkpoints=cps
    )
    tracker = LagTracker(event_store=es, checkpoints=cps)
    token = ConsistencyToken(position=2, projection="count")

    async def catch_up_soon() -> None:
        await asyncio.sleep(0.02)
        await rt.catch_up()

    task = asyncio.create_task(catch_up_soon())
    caught = await tracker.wait_for(token, timeout_s=2.0, poll_interval_s=0.005)
    await task
    assert caught is True


async def test_wait_for_times_out_when_behind() -> None:
    es = InMemoryEventStore()
    cps = InMemoryCheckpointStore()
    tracker = LagTracker(event_store=es, checkpoints=cps)
    token = ConsistencyToken(position=99, projection="count")
    caught = await tracker.wait_for(token, timeout_s=0.05, poll_interval_s=0.01)
    assert caught is False


# --------------------------------------------------------------------------- #
# Blue-green rebuilds
# --------------------------------------------------------------------------- #


async def test_blue_green_swaps_active_slot() -> None:
    es = InMemoryEventStore()
    rms = InMemoryReadModelStore()
    cps = InMemoryCheckpointStore()
    directory = InMemorySlotDirectory()
    for _ in range(3):
        await es.append("s", "tick", {})
    rebuilder = BlueGreenRebuilder(
        event_store=es,
        read_models=rms,
        checkpoints=cps,
        directory=directory,
    )
    proj = CountProjection()

    # Before any blue/green rebuild, reads resolve to the *bare* namespace (where
    # a normal runtime writes), not a coloured slot.
    assert await rebuilder.active_namespace("count") == "count"

    report = await rebuilder.rebuild(proj)
    assert report.from_slot == Slot.BLUE
    assert report.to_slot == Slot.GREEN
    assert report.swapped is True
    assert report.catch_up.applied == 3

    # Reads now resolve to GREEN, which holds the built view.
    active = await rebuilder.active_namespace("count")
    assert active == slot_namespace("count", Slot.GREEN)
    assert _val(await rms.get(active, "n")) == {"c": 3}


async def test_blue_green_keeps_old_slot_for_rollback() -> None:
    es = InMemoryEventStore()
    rms = InMemoryReadModelStore()
    cps = InMemoryCheckpointStore()
    directory = InMemorySlotDirectory()
    await es.append("s", "tick", {})
    rebuilder = BlueGreenRebuilder(
        event_store=es, read_models=rms, checkpoints=cps, directory=directory
    )
    await rebuilder.rebuild(CountProjection())  # blue -> green
    # Add an event then rebuild again: green -> blue, and the old green stays.
    await es.append("s", "tick", {})
    report = await rebuilder.rebuild(CountProjection())
    assert report.from_slot == Slot.GREEN
    assert report.to_slot == Slot.BLUE
    assert _val(await rms.get(slot_namespace("count", Slot.BLUE), "n")) == {"c": 2}
    # Old green slot retained (instant rollback) since clear_old defaulted False.
    assert await rms.count(slot_namespace("count", Slot.GREEN)) == 1


async def test_blue_green_clear_old_reclaims_space() -> None:
    es = InMemoryEventStore()
    rms = InMemoryReadModelStore()
    cps = InMemoryCheckpointStore()
    directory = InMemorySlotDirectory()
    await es.append("s", "tick", {})
    rebuilder = BlueGreenRebuilder(
        event_store=es, read_models=rms, checkpoints=cps, directory=directory
    )
    await rebuilder.rebuild(CountProjection(), clear_old=True)
    # The previously-active BLUE slot was cleared after the swap.
    assert await rms.count(slot_namespace("count", Slot.BLUE)) == 0
    assert await rms.count(slot_namespace("count", Slot.GREEN)) == 1
