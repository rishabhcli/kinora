"""RPO/RTO accounting + the backup health report."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.dr.accounting import health_report, rpo_rto_report
from app.dr.config import DRConfig
from app.dr.tiers import make_full, make_incremental
from tests.dr.fixtures import World


def test_rpo_rto_report_meets_objectives() -> None:
    config = DRConfig(rpo_target_s=300.0, rto_target_s=900.0)
    report = rpo_rto_report(
        recovery_point=10,
        source_head=12,
        recovery_point_time=1000.0,
        source_head_time=1100.0,  # 100s of data loss
        restore_duration_s=120.0,
        config=config,
    )
    assert report.rpo_s == 100.0
    assert report.rpo_met is True
    assert report.events_lost == 2
    assert report.rto_s == 120.0
    assert report.rto_met is True


def test_rpo_rto_report_breaches_objectives() -> None:
    config = DRConfig(rpo_target_s=60.0, rto_target_s=60.0)
    report = rpo_rto_report(
        recovery_point=5,
        source_head=20,
        recovery_point_time=1000.0,
        source_head_time=1500.0,  # 500s of data loss
        restore_duration_s=300.0,
        config=config,
    )
    assert report.rpo_met is False
    assert report.rto_met is False
    assert report.events_lost == 15


async def test_health_report_green_for_fresh_verified_fleet() -> None:
    world = World()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    world.add_shot("s1", page=1, recorded_at=10.0)
    world.add_shot("s2", page=2, recorded_at=20.0)
    full = await make_full(
        snapshot_id="full",
        event_source=world.events,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
        now=base,
    )
    await world.repo.save(full)

    config = DRConfig(overdue_after_s=3600.0, rpo_target_s=3600.0)
    report = await health_report(
        world.repo,
        config,
        now=base + timedelta(seconds=60),
        event_source=world.events,
    )
    assert report.total_backups == 1
    assert report.full_backups == 1
    assert report.integrity_ok is True
    # The freshest backup pins the head → achievable RPO is 0 (no unbacked events).
    assert report.achievable_rpo_s == 0.0
    assert report.rpo_objective_met is True
    assert report.findings == []


async def test_health_report_flags_unbacked_events_as_rpo_gap() -> None:
    world = World()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    world.add_shot("s1", page=1, recorded_at=10.0)
    full = await make_full(
        snapshot_id="full",
        event_source=world.events,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
        now=base,
    )
    await world.repo.save(full)
    # Two events land after the backup → an RPO gap of (90 - 10) = 80s.
    world.add_shot("s2", page=2, recorded_at=50.0)
    world.add_shot("s3", page=3, recorded_at=90.0)

    config = DRConfig(rpo_target_s=30.0, overdue_after_s=3600.0)
    report = await health_report(
        world.repo, config, now=base + timedelta(seconds=10), event_source=world.events
    )
    assert report.achievable_rpo_s == 80.0
    assert report.rpo_objective_met is False
    assert any("RPO" in f for f in report.findings)


async def test_health_report_flags_missing_full_backup() -> None:
    """A fleet of incrementals with no full is unrecoverable — flagged."""
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
        now=base + timedelta(seconds=5),
    )
    await world.repo.save(inc)
    await world.repo.delete("full")  # orphan the incremental

    report = await health_report(world.repo, DRConfig(), now=base, event_source=world.events)
    assert report.full_backups == 0
    assert report.integrity_ok is False
    assert any("full backup" in f for f in report.findings)


async def test_health_report_overdue_when_stale() -> None:
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
    report = await health_report(
        world.repo,
        DRConfig(overdue_after_s=60.0),
        now=base + timedelta(seconds=600),  # 10 min later
        event_source=world.events,
    )
    assert report.latest_backup_age_s == 600.0
    assert any("overdue" in f for f in report.findings)


async def test_health_report_empty_fleet_is_unprotected() -> None:
    world = World()
    report = await health_report(world.repo, DRConfig(), now=datetime(2026, 1, 1, tzinfo=UTC))
    assert report.total_backups == 0
    assert any("unprotected" in f for f in report.findings)
