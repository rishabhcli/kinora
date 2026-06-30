"""Restore engine: chain restore == source, dry-run, asset-mismatch detection."""

from __future__ import annotations

import pytest

from app.dr.checksums import digest
from app.dr.errors import AssetMismatchError
from app.dr.models import BackupManifest, SnapshotDescriptor
from app.dr.restore import plan_restore, restore
from app.dr.tiers import make_full, make_incremental
from tests.dr.fixtures import World, example_projector


async def _full(world: World, sid: str = "full") -> BackupManifest:
    m = await make_full(
        snapshot_id=sid,
        event_source=world.events,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
        checkpoints={"shot_board": await world.events.head_position()},
    )
    await world.repo.save(m)
    return m


async def _inc(
    world: World, parent: SnapshotDescriptor | BackupManifest, sid: str
) -> BackupManifest:
    m = await make_incremental(
        snapshot_id=sid,
        parent=parent,
        event_source=world.events,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
        checkpoints={"shot_board": await world.events.head_position()},
    )
    await world.repo.save(m)
    return m


async def test_full_restore_loads_captured_state_verbatim() -> None:
    world = World()
    world.add_entity("char_elsa", "Elsa", ref_key="refs/book/elsa.png")
    world.add_shot("s1", page=1)
    world.add_shot("s2", page=2)
    await world.rebuild_read_model_from_events()
    source_canon = await world.canon.dump()
    source_rm = await world.read_models.dump()

    full = await _full(world)

    # Wipe live state, then restore.
    world.canon.state = {"entities": {}, "episodic": {}}
    world.read_models.data.clear()

    plan, result = await restore(
        world.repo,
        full.descriptor.snapshot_id,
        event_sink=world.sink,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
    )
    assert result is not None
    assert result.verified
    assert result.restored_head == 2
    assert world.sink.head_position == 2
    # Canon + read models round-trip to the source exactly.
    assert digest(await world.canon.dump()) == digest(source_canon)
    assert digest(await world.read_models.dump()) == digest(source_rm)


async def test_full_plus_incremental_chain_restore_equals_source() -> None:
    """The headline guarantee: a full + N incrementals rebuilds the source state."""
    world = World()
    world.add_shot("s1", page=1)
    full = await _full(world)

    world.add_shot("s2", page=2)
    inc1 = await _inc(world, full, "inc1")

    world.add_shot("s3", page=3)
    world.add_shot("s4", page=4)
    inc2 = await _inc(world, inc1, "inc2")

    # Truth: rebuild the read model from ALL source events.
    await world.rebuild_read_model_from_events()
    source_canon = await world.canon.dump()
    source_rm = await world.read_models.dump()
    source_events = await world.events.read_range(0, await world.events.head_position())

    # Restore the chain head, rebuilding read models BY PROJECTION (truthful).
    world.canon.state = {"entities": {}, "episodic": {}}
    world.read_models.data.clear()
    plan, result = await restore(
        world.repo,
        inc2.descriptor.snapshot_id,
        event_sink=world.sink,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
        projector=example_projector,
    )
    assert result is not None and result.verified
    assert plan.chain_ids == ["full", "inc1", "inc2"]

    # The replayed log equals the source log, in order.
    assert [e["global_position"] for e in world.sink.events] == [
        e["global_position"] for e in source_events
    ]
    assert result.restored_head == 4
    # Rebuilt read models equal the source's; canon equals the head capture.
    assert digest(await world.read_models.dump()) == digest(source_rm)
    assert digest(await world.canon.dump()) == digest(source_canon)


async def test_dry_run_mutates_nothing_but_reports_a_plan() -> None:
    world = World()
    world.add_shot("s1", page=1)
    world.add_shot("s2", page=2)
    full = await _full(world)

    plan, result = await restore(
        world.repo,
        full.descriptor.snapshot_id,
        event_sink=world.sink,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
        dry_run=True,
    )
    assert result is None  # dry-run returns no result
    assert plan.restorable
    assert plan.events_to_replay == 2
    assert plan.read_model_rows >= 0
    assert plan.assets.ok
    # Nothing was written to the sink.
    assert world.sink.events == []


async def test_asset_mismatch_missing_asset_is_detected_and_aborts() -> None:
    world = World()
    world.add_shot("s1", page=1)
    await world.rebuild_read_model_from_events()
    full = await _full(world)

    # Lose a clip after capture.
    world.assets.remove("clips/book/s1.mp4")

    # A dry-run reports the missing asset without raising.
    plan = await plan_restore(world.repo, full.descriptor.snapshot_id, assets=world.assets)
    assert not plan.assets.ok
    assert "clips/book/s1.mp4" in plan.assets.missing

    # A real restore with require_assets aborts.
    with pytest.raises(AssetMismatchError) as exc:
        await restore(
            world.repo,
            full.descriptor.snapshot_id,
            event_sink=world.sink,
            canon=world.canon,
            read_models=world.read_models,
            assets=world.assets,
        )
    assert "clips/book/s1.mp4" in exc.value.missing


async def test_asset_mismatch_corrupted_asset_is_detected() -> None:
    """A silently-mutated asset (same key, different bytes) is caught by digest."""
    world = World()
    world.add_shot("s1", page=1)
    full = await _full(world)
    world.assets.corrupt("audio/book/s1.wav")

    plan = await plan_restore(world.repo, full.descriptor.snapshot_id, assets=world.assets)
    assert not plan.assets.ok
    assert "audio/book/s1.wav" in plan.assets.divergent


async def test_restore_can_proceed_when_assets_not_required() -> None:
    """An operator may force a restore past a missing asset (data over media)."""
    world = World()
    world.add_shot("s1", page=1)
    await world.rebuild_read_model_from_events()
    full = await _full(world)
    world.assets.remove("clips/book/s1.mp4")

    plan, result = await restore(
        world.repo,
        full.descriptor.snapshot_id,
        event_sink=world.sink,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
        require_assets=False,
    )
    assert result is not None and result.verified
    assert not plan.assets.ok  # the mismatch is still reported
