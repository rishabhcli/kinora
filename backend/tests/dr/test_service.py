"""DRService facade — the end-to-end backup → mutate → restore round-trip."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.dr.checksums import digest
from app.dr.config import DRConfig
from app.dr.errors import ChainError
from app.dr.service import DRService
from tests.dr.fixtures import World, example_projector


def _service(world: World, **kw: Any) -> DRService:
    seq = {"n": 0}

    def clock() -> datetime:
        seq["n"] += 1
        return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=seq["n"])

    return DRService(
        repo=world.repo,
        event_source=world.events,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
        event_sink=world.sink,
        clock=clock,
        **kw,
    )


async def test_full_backup_then_restore_roundtrip() -> None:
    world = World()
    world.add_entity("char_elsa", "Elsa", ref_key="refs/book/elsa.png")
    world.add_shot("s1", page=1)
    world.add_shot("s2", page=2)
    await world.rebuild_read_model_from_events()
    source_canon = await world.canon.dump()
    source_rm = await world.read_models.dump()

    svc = _service(world)
    backup = await svc.backup_full(labels={"trigger": "test"})
    assert backup.descriptor.snapshot_id == "bk_00000001"

    # Disaster: wipe the world.
    world.canon.state = {"entities": {}, "episodic": {}}
    world.read_models.data.clear()

    plan, result = await svc.restore()
    assert result is not None and result.verified
    assert digest(await world.canon.dump()) == digest(source_canon)
    assert digest(await world.read_models.dump()) == digest(source_rm)


async def test_backup_incremental_auto_chains_onto_latest() -> None:
    world = World()
    world.add_shot("s1", page=1)
    svc = _service(world)
    full = await svc.backup_full()

    world.add_shot("s2", page=2)
    inc = await svc.backup_incremental()
    assert inc.descriptor.parent_id == full.descriptor.snapshot_id
    assert inc.descriptor.base_position == full.descriptor.pinned_position

    # Restore the auto-resolved latest chain by projection.
    await world.rebuild_read_model_from_events()
    source_rm = await world.read_models.dump()
    world.read_models.data.clear()
    plan, result = await svc.restore(projector=example_projector)
    assert result is not None and result.verified
    assert plan.chain_ids == [full.descriptor.snapshot_id, inc.descriptor.snapshot_id]
    assert digest(await world.read_models.dump()) == digest(source_rm)


async def test_incremental_without_a_parent_is_rejected() -> None:
    world = World()
    world.add_shot("s1", page=1)
    svc = _service(world)
    with pytest.raises(ChainError):
        await svc.backup_incremental()


async def test_service_rpo_rto_reports_freshest_point() -> None:
    world = World()
    world.add_shot("s1", page=1, recorded_at=10.0)
    svc = _service(world, config=DRConfig(rpo_target_s=300.0, rto_target_s=600.0))
    await svc.backup_full()
    world.add_shot("s2", page=2, recorded_at=120.0)  # unbacked

    report = await svc.rpo_rto(restore_duration_s=30.0)
    assert report is not None
    assert report.recovery_point == 1  # the backup pinned position 1
    assert report.source_head == 2
    assert report.events_lost == 1
    assert report.rpo_s == 110.0  # 120 - 10
    assert report.rpo_met is True
    assert report.rto_met is True


async def test_service_gc_and_health_compose() -> None:
    world = World()
    # Build 3 chains via the service.
    svc = _service(world, config=DRConfig(keep_full=1, keep_incremental_chains=1))
    for chain_ix in range(3):
        world.add_shot(f"c{chain_ix}_a", page=1)
        await svc.backup_full()
        world.add_shot(f"c{chain_ix}_b", page=2)
        await svc.backup_incremental()

    health_before = await svc.health()
    assert health_before.total_backups == 6
    assert health_before.full_backups == 3

    plan = await svc.gc()
    assert len(plan.delete) > 0
    health_after = await svc.health()
    assert health_after.total_backups == len(plan.keep)
    # Integrity stays green after GC (no chain was orphaned).
    assert health_after.integrity_ok is True


async def test_restore_without_event_sink_is_rejected() -> None:
    world = World()
    world.add_shot("s1", page=1)
    svc = DRService(
        repo=world.repo,
        event_source=world.events,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
        event_sink=None,
    )
    await svc.backup_full()
    with pytest.raises(ChainError):
        await svc.restore()
