"""Retention policy + GC: prune old chains without ever orphaning a parent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.dr.config import DRConfig
from app.dr.retention import plan_gc, run_gc
from app.dr.tiers import make_full, make_incremental
from tests.dr.fixtures import World


def _at(base: datetime, minutes: float) -> datetime:
    return base + timedelta(minutes=minutes)


async def _build_fleet(world: World, base: datetime) -> None:
    """Three chains: full@0 + inc; full@10 + inc; full@20 + inc (newest)."""
    minute = 0.0
    for chain_ix in range(3):
        world.add_shot(f"c{chain_ix}_s1", page=1)
        full = await make_full(
            snapshot_id=f"full{chain_ix}",
            event_source=world.events,
            canon=world.canon,
            read_models=world.read_models,
            assets=world.assets,
            now=_at(base, minute),
        )
        await world.repo.save(full)
        minute += 5
        world.add_shot(f"c{chain_ix}_s2", page=2)
        inc = await make_incremental(
            snapshot_id=f"inc{chain_ix}",
            parent=full,
            event_source=world.events,
            canon=world.canon,
            read_models=world.read_models,
            assets=world.assets,
            now=_at(base, minute),
        )
        await world.repo.save(inc)
        minute += 5


async def test_gc_keeps_recent_full_chains_drops_old_ones() -> None:
    world = World()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    await _build_fleet(world, base)

    # Keep 2 full chains; keep incrementals only for the most recent 1 chain.
    config = DRConfig(keep_full=2, keep_incremental_chains=1)
    now = _at(base, 100)
    plan = plan_gc(
        [await world.repo.get(s) for s in await world.repo.list_ids()],  # type: ignore[misc]
        config,
        now=now,
    )

    # full0/inc0 are the oldest chain → retired whole.
    assert "full0" in plan.delete and "inc0" in plan.delete
    # full1 retained (within keep_full) but its incremental retired (beyond
    # keep_incremental_chains).
    assert "full1" in plan.keep and "inc1" in plan.delete
    # full2/inc2 are the newest chain → both retained.
    assert "full2" in plan.keep and "inc2" in plan.keep


async def test_gc_never_orphans_a_retained_incremental() -> None:
    """The invariant: no kept incremental loses its ancestor full."""
    world = World()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    await _build_fleet(world, base)
    config = DRConfig(keep_full=3, keep_incremental_chains=3)
    plan = plan_gc(
        [await world.repo.get(s) for s in await world.repo.list_ids()],  # type: ignore[misc]
        config,
        now=_at(base, 100),
    )
    deleted = set(plan.delete)
    kept = set(plan.keep)
    # Every kept incremental's full is also kept.
    for chain_ix in range(3):
        if f"inc{chain_ix}" in kept:
            assert f"full{chain_ix}" not in deleted


async def test_gc_freshness_floor_protects_young_backups() -> None:
    world = World()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    await _build_fleet(world, base)
    # Aggressive count policy that would drop everything but the newest chain,
    # but a 1000-minute freshness floor protects all (now is only 30 min later).
    config = DRConfig(keep_full=1, keep_incremental_chains=0, min_retain_age_s=1000 * 60)
    plan = plan_gc(
        [await world.repo.get(s) for s in await world.repo.list_ids()],  # type: ignore[misc]
        config,
        now=_at(base, 30),
    )
    assert plan.delete == []  # nothing old enough to collect


async def test_run_gc_applies_the_plan_to_the_repo() -> None:
    world = World()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    await _build_fleet(world, base)
    config = DRConfig(keep_full=1, keep_incremental_chains=1)
    plan = await run_gc(world.repo, config, now=_at(base, 100))
    remaining = set(await world.repo.list_ids())
    assert remaining == set(plan.keep)
    # The newest chain survived end-to-end.
    assert "full2" in remaining and "inc2" in remaining


async def test_orphan_backup_is_kept_for_review_not_deleted() -> None:
    """A backup whose lineage is broken is never auto-collected."""
    world = World()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    world.add_shot("s1", page=1)
    full = await make_full(
        snapshot_id="full",
        event_source=world.events,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
        now=base,
    )
    await world.repo.save(full)
    world.add_shot("s2", page=2)
    inc = await make_incremental(
        snapshot_id="inc",
        parent=full,
        event_source=world.events,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
        now=_at(base, 5),
    )
    # Save the incremental but NOT its full → orphan.
    await world.repo.save(inc)
    config = DRConfig(keep_full=1, keep_incremental_chains=1)
    plan = plan_gc([inc], config, now=_at(base, 100))
    assert "inc" in plan.keep
    assert plan.reasons["inc"] == "orphan-kept-for-review"
