"""Tests for snapshot-accelerated loads: the policy, the per-aggregate encode/
restore round-trip, and that a snapshot + tail replay reconstructs identical
state to a full replay (the load fast-path must be observationally equivalent)."""

from __future__ import annotations

from app.db.models.enums import EntityType, SessionMode
from app.eventsourcing.domain import commands_catalog as cc
from app.eventsourcing.domain.canon import CanonEntityAggregate
from app.eventsourcing.domain.render_shot import RenderShotAggregate, RenderState
from app.eventsourcing.domain.repository import Repository
from app.eventsourcing.domain.session import SessionAggregate
from app.eventsourcing.domain.snapshotting import (
    SnapshotPolicy,
    as_bool,
    as_float,
    as_int,
    as_str,
)
from app.eventsourcing.domain.wiring import build_command_bus
from app.eventsourcing.store.memory import InMemoryEventStore
from app.eventsourcing.store.snapshots import (
    InMemorySnapshotStore,
    Snapshot,
)


def test_snapshot_policy_triggers_on_multiple_crossing() -> None:
    p = SnapshotPolicy(every_n_events=5)
    assert p.should_snapshot(4, 5) is True  # crosses into the first multiple
    assert p.should_snapshot(5, 6) is False
    assert p.should_snapshot(9, 12) is True  # vaults a multiple in one save
    assert p.should_snapshot(0, 3) is False


def test_snapshot_policy_disabled() -> None:
    assert SnapshotPolicy(every_n_events=0).should_snapshot(0, 100) is False


def test_coercion_helpers_are_total() -> None:
    assert as_int("nope", 7) == 7
    assert as_int(True, 7) == 7  # bool is not int here
    assert as_int(3) == 3
    assert as_float("x", 1.5) == 1.5
    assert as_float(2) == 2.0
    assert as_str(5, "d") == "d"
    assert as_str("ok") == "ok"
    assert as_bool("x", True) is True
    assert as_bool(False) is False


def test_in_memory_snapshot_store_keeps_highest_version() -> None:
    store = InMemorySnapshotStore()

    async def run() -> None:
        await store.save(Snapshot("s", 10, {"a": 1}))
        await store.save(Snapshot("s", 5, {"a": 0}))  # older, ignored
        snap = await store.load("s")
        assert snap is not None
        assert snap.version == 10

    import asyncio

    asyncio.run(run())


async def test_session_snapshot_round_trip() -> None:
    agg = SessionAggregate("s1")
    agg.start(user_id="u1", book_id="b1")
    agg.switch_mode(mode=SessionMode.DIRECTOR)
    agg.record_preference(key="pacing", value="slow")
    state = agg.snapshot_state()

    restored = SessionAggregate("s1")
    restored.restore_state(state, version=agg.version)
    assert restored.user_id == "u1"
    assert restored.mode is SessionMode.DIRECTOR
    assert restored.preferences == {"pacing": "slow"}
    assert restored.version == agg.version
    assert restored.expected_version == agg.version


async def test_render_shot_snapshot_round_trip() -> None:
    agg = RenderShotAggregate("shot1")
    agg.plan(book_id="b", scene_id="s", beat_id="be", shot_hash="h1")
    agg.promote()
    agg.begin_cache_check()
    agg.cache_miss()
    agg.rendered(clip_url="c", video_seconds=5.0)
    state = agg.snapshot_state()

    restored = RenderShotAggregate("shot1")
    restored.restore_state(state, version=agg.version)
    assert restored.state is RenderState.QA
    assert restored.video_seconds_spent == 5.0
    assert restored.shot_hash == "h1"


async def test_canon_snapshot_round_trip() -> None:
    agg = CanonEntityAggregate("e1")
    agg.register(book_id="b", entity_type=EntityType.LOCATION, name="Manor")
    agg.edit_field(field_name="mood", new_value="gloomy")
    state = agg.snapshot_state()

    restored = CanonEntityAggregate("e1")
    restored.restore_state(state, version=agg.version)
    assert restored.entity_type is EntityType.LOCATION
    assert restored.fields == {"mood": "gloomy"}
    assert restored.canon_version == 2


async def test_repository_snapshot_then_tail_equals_full_replay() -> None:
    store = InMemoryEventStore()
    snaps = InMemorySnapshotStore()
    repo: Repository[SessionAggregate] = Repository(
        store,
        SessionAggregate,
        snapshot_store=snaps,
        snapshot_policy=SnapshotPolicy(every_n_events=2),
    )

    # Build a session with several events, saving incrementally so snapshots write.
    agg = SessionAggregate("s1")
    agg.start(user_id="u1", book_id="b1")
    await repo.save(agg)
    agg.switch_mode(mode=SessionMode.DIRECTOR)
    await repo.save(agg)  # version 2 -> snapshot written
    agg.record_preference(key="palette", value="warm")
    await repo.save(agg)
    agg.record_preference(key="pacing", value="slow")
    await repo.save(agg)  # version 4 -> snapshot written

    # A snapshot exists at a version below the head.
    snap = await snaps.load("session-s1")
    assert snap is not None
    assert snap.version >= 2

    # Loading via the snapshot fast-path reconstructs identical state...
    via_snapshot = await repo.load("s1")
    # ...as a full replay through a snapshot-less repo over the same store.
    plain_repo: Repository[SessionAggregate] = Repository(store, SessionAggregate)
    via_full = await plain_repo.load("s1")

    assert via_snapshot.snapshot_state() == via_full.snapshot_state()
    assert via_snapshot.version == via_full.version
    assert via_snapshot.preferences == {"palette": "warm", "pacing": "slow"}


async def test_bus_with_snapshots_round_trips_through_load() -> None:
    store = InMemoryEventStore()
    snaps = InMemorySnapshotStore()
    bus = build_command_bus(
        store, snapshot_store=snaps, snapshot_policy=SnapshotPolicy(every_n_events=2)
    )
    # Drive a render-shot through several commands (each appends multiple events,
    # so snapshots get written along the way).
    await bus.dispatch(
        cc.PlanShot(shot_id="sh1", book_id="b", scene_id="s", beat_id="be", shot_hash="h1")
    )
    await bus.dispatch(cc.PromoteShot(shot_id="sh1"))
    await bus.dispatch(
        cc.RenderShot(
            shot_id="sh1", shot_hash="h1", cache_hit=False, clip_url="c", video_seconds=5.0
        )
    )
    await bus.dispatch(cc.ScoreShotQA(shot_id="sh1", score=0.95, passed=True))

    snap = await snaps.load("rendershot-sh1")
    assert snap is not None  # snapshots were written during the flow

    repo: Repository[RenderShotAggregate] = Repository(
        store, RenderShotAggregate, snapshot_store=snaps
    )
    loaded = await repo.load("sh1")
    assert loaded.state is RenderState.ACCEPTED
    assert loaded.video_seconds_spent == 5.0


async def test_load_without_snapshot_store_is_full_replay() -> None:
    store = InMemoryEventStore()
    repo: Repository[SessionAggregate] = Repository(store, SessionAggregate)
    agg = SessionAggregate("s1")
    agg.start(user_id="u1", book_id="b1")
    await repo.save(agg)
    loaded = await repo.load("s1")
    assert loaded.started is True
    assert loaded.user_id == "u1"
