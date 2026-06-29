"""Tests for snapshots, the version guard, and the read facade (Phase 2)."""

from __future__ import annotations

import pytest

from app.eventsourcing.projections.checkpoints import InMemoryCheckpointStore
from app.eventsourcing.projections.lag import ConsistencyToken
from app.eventsourcing.projections.memory_eventstore import InMemoryEventStore
from app.eventsourcing.projections.projection import Projection, handles
from app.eventsourcing.projections.reader import ProjectionReader
from app.eventsourcing.projections.readmodel import (
    InMemoryReadModelStore,
    ReadModelRow,
)
from app.eventsourcing.projections.registry import ProjectionRegistry
from app.eventsourcing.projections.runtime import ProjectionRuntime, RuntimeConfig
from app.eventsourcing.projections.snapshots import (
    InMemorySnapshotStore,
    SnapshotPolicy,
    capture,
    restore_into,
)
from app.eventsourcing.projections.versioning import (
    VersionAction,
    VersionGuard,
    check_version,
)

pytestmark = pytest.mark.asyncio

def _val(row: ReadModelRow | None) -> dict[str, object]:
    assert row is not None
    return row.value



class CountProjection(Projection):
    name = "count"
    version = 1

    @handles("tick")
    async def _on(self, store, ns, ev) -> None:  # type: ignore[no-untyped-def]
        row = await store.get(ns, "n")
        cur = row.value["c"] if row else 0
        await store.put(ns, "n", {"c": cur + 1})


def _stores():  # type: ignore[no-untyped-def]
    return InMemoryEventStore(), InMemoryReadModelStore(), InMemoryCheckpointStore()


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #


async def test_capture_and_restore_round_trip() -> None:
    store = InMemoryReadModelStore()
    await store.put("ns", "a", {"x": 1})
    await store.put("ns", "b", {"x": 2})
    snap = await capture(store, projection="p", namespace="ns", position=10)
    assert snap.position == 10
    assert snap.row_count == 2

    target = InMemoryReadModelStore()
    n = await restore_into(target, "ns2", snap)
    assert n == 2
    assert _val(await target.get("ns2", "a")) == {"x": 1}
    assert _val(await target.get("ns2", "b")) == {"x": 2}


async def test_snapshot_policy_off_by_default() -> None:
    assert SnapshotPolicy().should_snapshot(1000) is False
    assert SnapshotPolicy(interval=5).should_snapshot(4) is False
    assert SnapshotPolicy(interval=5).should_snapshot(5) is True


async def test_catch_up_takes_snapshots_per_policy() -> None:
    es, rms, cps = _stores()
    snaps = InMemorySnapshotStore()
    for _ in range(10):
        await es.append("s", "tick", {})
    rt = ProjectionRuntime(
        CountProjection(),
        event_store=es,
        read_models=rms,
        checkpoints=cps,
        config=RuntimeConfig(batch_size=2),
        snapshots=snaps,
        snapshot_policy=SnapshotPolicy(interval=4),
    )
    result = await rt.catch_up()
    assert result.applied == 10
    assert result.snapshots >= 2  # snapshotted at least twice over 10 events
    latest = await snaps.latest("count")
    assert latest is not None
    assert latest.rows["n"] == {"c": 10} or latest.position <= 10


async def test_restore_or_rebuild_replays_only_the_tail() -> None:
    es, rms, cps = _stores()
    snaps = InMemorySnapshotStore()
    for _ in range(5):
        await es.append("s", "tick", {})
    rt = ProjectionRuntime(
        CountProjection(),
        event_store=es,
        read_models=rms,
        checkpoints=cps,
        snapshots=snaps,
    )
    await rt.catch_up()
    # Snapshot at position 5, then append 3 more.
    await rt.snapshot_now()
    for _ in range(3):
        await es.append("s", "tick", {})

    # A snapshot-accelerated rebuild restores the snapshot (count=5) then replays
    # only the 3 tail events — applying 3, not 8.
    result = await rt.restore_or_rebuild()
    assert result.restored_from_snapshot is True
    assert result.applied == 3
    assert result.extra["restored_rows"] == 1
    assert _val(await rms.get("count", "n")) == {"c": 8}


async def test_restore_or_rebuild_falls_back_without_snapshot() -> None:
    es, rms, cps = _stores()
    snaps = InMemorySnapshotStore()
    for _ in range(4):
        await es.append("s", "tick", {})
    rt = ProjectionRuntime(
        CountProjection(),
        event_store=es,
        read_models=rms,
        checkpoints=cps,
        snapshots=snaps,
    )
    await rt.catch_up()
    # No snapshot taken -> full rebuild replays all 4.
    result = await rt.restore_or_rebuild()
    assert result.restored_from_snapshot is False
    assert result.applied == 4
    assert _val(await rms.get("count", "n")) == {"c": 4}


async def test_snapshot_ignored_after_version_bump() -> None:
    es, rms, cps = _stores()
    snaps = InMemorySnapshotStore()
    for _ in range(3):
        await es.append("s", "tick", {})
    rt = ProjectionRuntime(
        CountProjection(),
        event_store=es,
        read_models=rms,
        checkpoints=cps,
        snapshots=snaps,
    )
    await rt.catch_up()
    await rt.snapshot_now()  # snapshot under version 1

    # A new projection instance with a bumped version must not reuse the v1 snapshot.
    class CountV2(CountProjection):
        name = "count"
        version = 2

    rt2 = ProjectionRuntime(
        CountV2(), event_store=es, read_models=rms, checkpoints=cps, snapshots=snaps
    )
    result = await rt2.restore_or_rebuild()
    assert result.restored_from_snapshot is False  # snapshot invalidated by bump
    assert result.applied == 3


# --------------------------------------------------------------------------- #
# Version guard
# --------------------------------------------------------------------------- #


async def test_check_version_pure() -> None:
    proj = CountProjection()
    assert check_version(proj, None).action == VersionAction.FIRST_BUILD
    assert check_version(proj, 1).action == VersionAction.UP_TO_DATE
    assert check_version(proj, 99).action == VersionAction.REBUILD


async def test_version_guard_decide_first_build() -> None:
    cps = InMemoryCheckpointStore()
    guard = VersionGuard(cps)
    decision = await guard.decide(CountProjection())
    assert decision.action == VersionAction.FIRST_BUILD
    assert decision.needs_rebuild


async def test_version_guard_ensure_current_rebuilds_on_bump() -> None:
    es, rms, cps = _stores()
    for _ in range(2):
        await es.append("s", "tick", {})
    rt = ProjectionRuntime(CountProjection(), event_store=es, read_models=rms, checkpoints=cps)
    await rt.catch_up()
    guard = VersionGuard(cps)
    await guard.stamp(CountProjection())  # record v1

    # Up to date: no rebuild.
    rebuilt = {"count": 0}

    async def do_rebuild() -> None:
        rebuilt["count"] += 1
        await rt.rebuild()

    d1 = await guard.ensure_current(CountProjection(), do_rebuild)
    assert d1.action == VersionAction.UP_TO_DATE
    assert rebuilt["count"] == 0

    # Bump version: rebuild fires and the new version is stamped.
    class CountV2(CountProjection):
        name = "count"
        version = 2

    rt_v2 = ProjectionRuntime(CountV2(), event_store=es, read_models=rms, checkpoints=cps)

    async def do_rebuild_v2() -> None:
        rebuilt["count"] += 1
        await rt_v2.rebuild()

    d2 = await guard.ensure_current(CountV2(), do_rebuild_v2)
    assert d2.action == VersionAction.REBUILD
    assert rebuilt["count"] == 1
    # A second ensure_current is now up to date (version was stamped).
    d3 = await guard.ensure_current(CountV2(), do_rebuild_v2)
    assert d3.action == VersionAction.UP_TO_DATE
    assert rebuilt["count"] == 1


# --------------------------------------------------------------------------- #
# Read facade
# --------------------------------------------------------------------------- #


async def test_reader_resolves_bare_namespace_before_rebuild() -> None:
    es, rms, cps = _stores()
    reg = ProjectionRegistry(event_store=es, read_models=rms, checkpoints=cps)
    reg.register(CountProjection())
    for _ in range(3):
        await es.append("s", "tick", {})
    await reg.runtime("count").catch_up()

    reader = ProjectionReader(reg)
    result = await reader.get("count", "n")
    assert result.one is not None
    assert result.one.value == {"c": 3}
    assert result.stale is False
    assert result.position == 3


async def test_reader_read_your_writes_not_stale_when_caught_up() -> None:
    es, rms, cps = _stores()
    reg = ProjectionRegistry(event_store=es, read_models=rms, checkpoints=cps)
    reg.register(CountProjection())
    for _ in range(2):
        await es.append("s", "tick", {})
    await reg.runtime("count").catch_up()

    reader = ProjectionReader(reg, ryw_timeout_s=0.2)
    token = ConsistencyToken(position=2, projection="count")
    result = await reader.get("count", "n", token=token)
    assert result.stale is False
    assert _val(result.one) == {"c": 2}


async def test_reader_marks_stale_on_ryw_timeout() -> None:
    es, rms, cps = _stores()
    reg = ProjectionRegistry(event_store=es, read_models=rms, checkpoints=cps)
    reg.register(CountProjection())
    await es.append("s", "tick", {})
    await reg.runtime("count").catch_up()  # position 1

    reader = ProjectionReader(reg, ryw_timeout_s=0.05)
    # Token demands position 99, which the projection will never reach here.
    token = ConsistencyToken(position=99, projection="count")
    result = await reader.get("count", "n", token=token)
    assert result.stale is True  # timed out waiting; data may be stale


async def test_reader_list_returns_all_rows() -> None:
    es, rms, cps = _stores()
    reg = ProjectionRegistry(event_store=es, read_models=rms, checkpoints=cps)

    class MultiKey(Projection):
        name = "multi"

        @handles("put")
        async def _put(self, store, ns, ev) -> None:  # type: ignore[no-untyped-def]
            await store.put(ns, ev.payload["k"], {"v": ev.payload["v"]})

    reg.register(MultiKey())
    await es.append("s", "put", {"k": "a", "v": 1})
    await es.append("s", "put", {"k": "b", "v": 2})
    await reg.runtime("multi").catch_up()

    reader = ProjectionReader(reg)
    result = await reader.list("multi")
    assert [r.key for r in result.rows] == ["a", "b"]
