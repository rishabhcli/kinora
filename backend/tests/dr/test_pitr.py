"""Point-in-time recovery — restore to event position / timestamp ``T``."""

from __future__ import annotations

import pytest

from app.dr.errors import PointInTimeError
from app.dr.pitr import (
    recover_to_position,
    recover_to_timestamp,
    resolve_target_position,
    resolve_target_timestamp,
)
from app.dr.tiers import make_full, make_incremental
from tests.dr.fixtures import World, example_projector


async def _seed_chain(world: World) -> None:
    """A full at pin 2, an incremental at pin 4, with timestamps 1..4."""
    world.add_shot("s1", page=1, recorded_at=100.0)
    world.add_shot("s2", page=2, recorded_at=200.0)
    full = await make_full(
        snapshot_id="full",
        event_source=world.events,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
    )
    await world.repo.save(full)
    world.add_shot("s3", page=3, recorded_at=300.0)
    world.add_shot("s4", page=4, recorded_at=400.0)
    inc = await make_incremental(
        snapshot_id="inc",
        parent=full,
        event_source=world.events,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
    )
    await world.repo.save(inc)


async def test_resolve_position_picks_freshest_covering_chain() -> None:
    world = World()
    await _seed_chain(world)
    # Position 3 is covered only by the incremental (pin 4), not the full (pin 2).
    target = await resolve_target_position(world.repo, 3)
    assert target.head_id == "inc"
    assert target.replay_through == 3
    # Position 2 is covered by the full (smallest pin >= 2).
    target2 = await resolve_target_position(world.repo, 2)
    assert target2.head_id == "full"
    assert target2.replay_through == 2


async def test_recover_to_position_truncates_the_log() -> None:
    """PITR to position 3 replays events 1..3 and rebuilds read models to match."""
    world = World()
    await _seed_chain(world)

    target, plan, result = await recover_to_position(
        world.repo,
        3,
        event_sink=world.sink,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
        projector=example_projector,
    )
    assert result is not None and result.verified
    assert target.replay_through == 3
    assert [e["global_position"] for e in world.sink.events] == [1, 2, 3]
    assert result.restored_head == 3
    # The read model reflects exactly the 3 recovered shots (s4 is gone).
    board = world.read_models.data.get("shot_board", {})
    assert set(board) == {"s1", "s2", "s3"}


async def test_recover_to_timestamp_maps_time_to_position() -> None:
    world = World()
    await _seed_chain(world)
    # T = 350.0 lands between s3 (300) and s4 (400) → recover through position 3.
    target, plan, result = await recover_to_timestamp(
        world.repo,
        world.events,
        350.0,
        event_sink=world.sink,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
        projector=example_projector,
    )
    assert result is not None and result.verified
    assert target.resolved_from == "timestamp"
    assert target.replay_through == 3
    assert [e["global_position"] for e in world.sink.events] == [1, 2, 3]


async def test_resolve_timestamp_at_exact_event_time_includes_it() -> None:
    world = World()
    await _seed_chain(world)
    # T exactly at s2's time → position 2 inclusive.
    target = await resolve_target_timestamp(world.repo, world.events, 200.0)
    assert target.replay_through == 2


async def test_position_past_freshest_backup_is_rejected() -> None:
    world = World()
    await _seed_chain(world)
    # Append a 5th event that no backup covers yet.
    world.add_shot("s5", page=5, recorded_at=500.0)
    with pytest.raises(PointInTimeError):
        await resolve_target_position(world.repo, 5)


async def test_recovery_with_no_backups_is_rejected() -> None:
    world = World()
    with pytest.raises(PointInTimeError):
        await resolve_target_position(world.repo, 1)


async def test_timestamp_before_first_event_recovers_empty_world() -> None:
    world = World()
    await _seed_chain(world)
    # T precedes every event → recover the empty world (position 0).
    target = await resolve_target_timestamp(world.repo, world.events, 1.0)
    assert target.replay_through == 0


async def test_pitr_dry_run_does_not_mutate() -> None:
    world = World()
    await _seed_chain(world)
    target, plan, result = await recover_to_position(
        world.repo,
        3,
        event_sink=world.sink,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
        projector=example_projector,
        dry_run=True,
    )
    assert result is None
    assert plan.events_to_replay == 3
    assert world.sink.events == []
