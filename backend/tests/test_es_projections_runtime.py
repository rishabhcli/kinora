"""Unit tests for the projection runtime + stores (no infra; in-memory fakes).

Covers the at-least-once + idempotent delivery contract, catch-up paging, the
live tail, type-filtered catch-up, error handling / retry / dead-letter, and the
in-memory store implementations themselves. The DB-backed stores are exercised
separately in ``test_es_projections_pg.py`` (skipped without infra).
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.eventsourcing.projections.checkpoints import (
    InMemoryCheckpointStore,
    ProjectionStatus,
)
from app.eventsourcing.projections.contracts import StoredEvent
from app.eventsourcing.projections.memory_eventstore import InMemoryEventStore
from app.eventsourcing.projections.projection import Projection, handles
from app.eventsourcing.projections.readmodel import (
    InMemoryReadModelStore,
    ReadModelRow,
    ReadModelStore,
)
from app.eventsourcing.projections.runtime import (
    ProjectionFaultedError,
    ProjectionRuntime,
    RuntimeConfig,
)

pytestmark = pytest.mark.asyncio

Stores = tuple[InMemoryEventStore, InMemoryReadModelStore, InMemoryCheckpointStore]


def _val(row: ReadModelRow | None) -> dict[str, object]:
    """Assert a row exists and return its value (keeps mypy happy on Optional)."""
    assert row is not None
    return row.value


# --------------------------------------------------------------------------- #
# Test projections
# --------------------------------------------------------------------------- #


class CountProjection(Projection):
    """Counts ``tick`` events into a single row (relative increment)."""

    name = "count"

    @handles("tick")
    async def _on_tick(
        self, store: ReadModelStore, namespace: str, event: StoredEvent
    ) -> None:
        row = await store.get(namespace, "n")
        current = row.value["c"] if row else 0
        await store.put(namespace, "n", {"c": current + 1})


class LastValueProjection(Projection):
    """Stores the latest ``set`` payload (absolute upsert; only cares about ``set``)."""

    name = "last_value"

    @handles("set")
    async def _on_set(
        self, store: ReadModelStore, namespace: str, event: StoredEvent
    ) -> None:
        await store.put(namespace, event.stream_id, {"v": event.payload["v"]})


class PoisonProjection(Projection):
    """Raises on a ``boom`` event to exercise the error/retry path."""

    name = "poison"

    def __init__(self) -> None:
        self.calls = 0

    @handles("ok")
    async def _on_ok(
        self, store: ReadModelStore, namespace: str, event: StoredEvent
    ) -> None:
        await store.put(namespace, event.stream_id, {"ok": True})

    @handles("boom")
    async def _on_boom(
        self, store: ReadModelStore, namespace: str, event: StoredEvent
    ) -> None:
        self.calls += 1
        raise RuntimeError("kaboom")


@pytest.fixture
def stores() -> Stores:
    return InMemoryEventStore(), InMemoryReadModelStore(), InMemoryCheckpointStore()


def make_runtime(
    projection: Projection,
    stores: Stores,
    *,
    config: RuntimeConfig | None = None,
    dead_letter: object = None,
) -> ProjectionRuntime:
    es, rms, cps = stores
    kw: dict[str, object] = {}
    if config is not None:
        kw["config"] = config
    if dead_letter is not None:
        kw["dead_letter"] = dead_letter
    return ProjectionRuntime(
        projection,
        event_store=es,
        read_models=rms,
        checkpoints=cps,
        **kw,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Catch-up + idempotency
# --------------------------------------------------------------------------- #


async def test_catch_up_applies_all_events(stores: Stores) -> None:
    es, rms, _ = stores
    for _ in range(5):
        await es.append("s", "tick", {})
    rt = make_runtime(CountProjection(), stores)
    result = await rt.catch_up()
    assert result.applied == 5
    assert result.final_position == 5
    assert _val(await rms.get("count", "n")) == {"c": 5}


async def test_catch_up_is_idempotent_on_replay(stores: Stores) -> None:
    es, rms, _ = stores
    for _ in range(3):
        await es.append("s", "tick", {})
    rt = make_runtime(CountProjection(), stores)
    await rt.catch_up()
    second = await rt.catch_up()
    assert second.applied == 0  # nothing new
    assert _val(await rms.get("count", "n")) == {"c": 3}  # not double-counted


async def test_dedupe_skips_already_applied_event(stores: Stores) -> None:
    """A re-delivered event_id is dropped before the handler runs."""
    es, rms, cps = stores
    e = await es.append("s", "tick", {})
    rt = make_runtime(CountProjection(), stores)
    await rt.catch_up()
    assert await cps.was_applied("count", e.event_id)
    assert _val(await rms.get("count", "n")) == {"c": 1}

    # Simulate at-least-once redelivery: the checkpoint persists, but the applied
    # ledger is the real guard. Reset only the *position* so the runtime re-reads
    # and re-delivers the same event — the dedupe must drop it.
    await cps.reset("count")  # clears position AND the applied ledger
    await cps.mark_applied("count", e.event_id)  # but pretend it was already applied
    again = await rt.catch_up()
    assert again.applied == 0
    assert again.skipped == 1  # dropped by dedupe
    assert _val(await rms.get("count", "n")) == {"c": 1}  # not double-counted


async def test_checkpoint_advances_to_head(stores: Stores) -> None:
    es, _, cps = stores
    for _ in range(7):
        await es.append("s", "tick", {})
    rt = make_runtime(CountProjection(), stores)
    await rt.catch_up()
    cp = await cps.load("count")
    assert cp.position == 7
    assert cp.status == ProjectionStatus.LIVE
    assert cp.lag == 0


async def test_paging_with_small_batch(stores: Stores) -> None:
    es, rms, _ = stores
    for _ in range(10):
        await es.append("s", "tick", {})
    rt = make_runtime(CountProjection(), stores, config=RuntimeConfig(batch_size=3))
    result = await rt.catch_up()
    assert result.applied == 10
    assert _val(await rms.get("count", "n")) == {"c": 10}


# --------------------------------------------------------------------------- #
# Type-filtered catch-up (the lag-correctness case)
# --------------------------------------------------------------------------- #


async def test_filtered_projection_checkpoints_past_irrelevant_tail(stores: Stores) -> None:
    """A type-filtered projection advances to head, not the last matched event."""
    es, rms, cps = stores
    await es.append("s", "set", {"v": 1})  # pos 1 (matches)
    for _ in range(5):
        await es.append("s", "noise", {})  # pos 2..6 (filtered out)
    rt = make_runtime(LastValueProjection(), stores)
    result = await rt.catch_up()
    assert result.applied == 1
    cp = await cps.load("last_value")
    # Checkpoint must reach the head (6), so lag reads 0 — not stuck at pos 1.
    assert cp.position == 6
    assert cp.lag == 0
    assert _val(await rms.get("last_value", "s")) == {"v": 1}


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


async def test_stop_on_error_raises_and_faults(stores: Stores) -> None:
    es, _, cps = stores
    await es.append("s", "ok", {})
    await es.append("s", "boom", {})
    proj = PoisonProjection()
    rt = make_runtime(proj, stores, config=RuntimeConfig(max_retries=2, retry_backoff_s=0))
    with pytest.raises(ProjectionFaultedError) as exc:
        await rt.catch_up()
    assert exc.value.event.type == "boom"
    assert proj.calls == 2  # retried max_retries times
    cp = await cps.load("poison")
    assert cp.status == ProjectionStatus.FAULTED
    assert cp.error_count == 1


async def test_non_fatal_mode_dead_letters_and_continues(stores: Stores) -> None:
    es, rms, _ = stores
    await es.append("s1", "ok", {})
    await es.append("s2", "boom", {})
    await es.append("s3", "ok", {})
    dead: list[str] = []

    async def sink(name: str, event: StoredEvent, exc: Exception) -> None:
        dead.append(event.event_id)

    rt = make_runtime(
        PoisonProjection(),
        stores,
        config=RuntimeConfig(max_retries=1, retry_backoff_s=0, stop_on_error=False),
        dead_letter=sink,
    )
    result = await rt.catch_up()
    assert result.applied == 2  # both ok events
    assert result.dead_lettered == 1
    assert len(dead) == 1
    # The projection made progress past the poison event.
    assert _val(await rms.get("poison", "s1")) == {"ok": True}
    assert _val(await rms.get("poison", "s3")) == {"ok": True}


# --------------------------------------------------------------------------- #
# Rebuild + live tail
# --------------------------------------------------------------------------- #


async def test_rebuild_clears_and_replays(stores: Stores) -> None:
    es, rms, _ = stores
    for _ in range(4):
        await es.append("s", "tick", {})
    rt = make_runtime(CountProjection(), stores)
    await rt.catch_up()
    assert _val(await rms.get("count", "n")) == {"c": 4}
    # Rebuild re-folds from scratch into a cleared namespace.
    result = await rt.rebuild()
    assert result.applied == 4
    assert _val(await rms.get("count", "n")) == {"c": 4}


async def test_live_tail_applies_new_events(stores: Stores) -> None:
    es, rms, _ = stores
    await es.append("s", "tick", {})
    rt = make_runtime(CountProjection(), stores, config=RuntimeConfig(poll_interval_s=0.01))
    stop = asyncio.Event()
    task = asyncio.create_task(rt.run(stop_event=stop))
    # Append more after the tail starts.
    await asyncio.sleep(0.02)
    await es.append("s", "tick", {})
    await es.append("s", "tick", {})
    # Wait until the projection catches up.
    for _ in range(100):
        row = await rms.get("count", "n")
        if row and row.value["c"] == 3:
            break
        await asyncio.sleep(0.01)
    stop.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert _val(await rms.get("count", "n")) == {"c": 3}


# --------------------------------------------------------------------------- #
# Store + projection-base unit tests
# --------------------------------------------------------------------------- #


async def test_readmodel_store_versions_and_isolation() -> None:
    store = InMemoryReadModelStore()
    r1 = await store.put("ns", "k", {"x": 1})
    assert r1.version == 1
    r2 = await store.put("ns", "k", {"x": 2})
    assert r2.version == 2
    # Mutating a returned value must not corrupt stored state.
    got = await store.get("ns", "k")
    assert got is not None
    got.value["x"] = 999
    again = await store.get("ns", "k")
    assert again is not None and again.value["x"] == 2


async def test_readmodel_store_list_prefix_and_clear() -> None:
    store = InMemoryReadModelStore()
    await store.put("ns", "a:1", {})
    await store.put("ns", "a:2", {})
    await store.put("ns", "b:1", {})
    rows = await store.list("ns", prefix="a:")
    assert [r.key for r in rows] == ["a:1", "a:2"]
    assert await store.count("ns") == 3
    removed = await store.clear("ns")
    assert removed == 3
    assert await store.count("ns") == 0


async def test_projection_rejects_duplicate_handler() -> None:
    with pytest.raises(ValueError, match="duplicate handler"):

        class Bad(Projection):
            name = "bad"

            @handles("x")
            async def a(
                self, store: ReadModelStore, ns: str, ev: StoredEvent
            ) -> None: ...

            @handles("x")
            async def b(
                self, store: ReadModelStore, ns: str, ev: StoredEvent
            ) -> None: ...


async def test_projection_interested_in_reflects_handlers() -> None:
    assert CountProjection().interested_in() == frozenset({"tick"})
    assert PoisonProjection().interested_in() == frozenset({"ok", "boom"})


async def test_checkpoint_advance_is_forward_only() -> None:
    cps = InMemoryCheckpointStore()
    await cps.advance("p", 5)
    await cps.advance("p", 2)  # backwards: ignored
    assert (await cps.load("p")).position == 5
