"""Tests for the online resharding state machine (no infra).

These prove the *protocol* correctness with an in-memory mover: dual-write,
batched backfill, verify, atomic cutover, cleanup, and rollback-before-cutover.
"""

from __future__ import annotations

import pytest

from app.datascale.sharding.keys import ShardKey
from app.datascale.sharding.resharding import (
    InMemoryReshardMover,
    ReshardError,
    ReshardingJob,
    ReshardPlan,
    ReshardState,
)
from app.datascale.sharding.router import MigrationOverlay

pytestmark = pytest.mark.asyncio


def _keys(*names: str) -> tuple[ShardKey, ...]:
    return tuple(ShardKey.of(n) for n in names)


def _seed_source(rows: dict[str, tuple[str, str]]) -> InMemoryReshardMover:
    """Seed the source shard 'old' with ``{row_id: (key_name, payload)}``."""
    table: dict[str, tuple[ShardKey, str]] = {
        rid: (ShardKey.of(kn), payload) for rid, (kn, payload) in rows.items()
    }
    return InMemoryReshardMover(data={"old": {"books": table}, "new": {"books": {}}})


def _plan(keys: tuple[ShardKey, ...], batch_size: int = 500) -> ReshardPlan:
    return ReshardPlan(table="books", keys=keys, source="old", target="new", batch_size=batch_size)


async def test_full_reshard_moves_rows_and_cuts_over() -> None:
    mover = _seed_source(
        {
            "r1": ("tenant-A", "p1"),
            "r2": ("tenant-A", "p2"),
            "r3": ("tenant-B", "p3"),  # not moving
        }
    )
    job = ReshardingJob(plan=_plan(_keys("tenant-A")), mover=mover)
    progress = await job.run()

    assert progress.state is ReshardState.DONE
    assert progress.verified
    assert progress.rows_backfilled == 2
    assert progress.rows_deleted == 2
    # tenant-A rows now live on 'new', removed from 'old'; tenant-B untouched.
    assert set(mover.data["new"]["books"].keys()) == {"r1", "r2"}
    assert set(mover.data["old"]["books"].keys()) == {"r3"}


async def test_state_machine_visits_phases_in_order() -> None:
    mover = _seed_source({"r1": ("t", "p")})
    job = ReshardingJob(plan=_plan(_keys("t")), mover=mover)
    await job.run()
    assert job.progress.history == [
        ReshardState.PLANNING,
        ReshardState.DUAL_WRITE,
        ReshardState.BACKFILL,
        ReshardState.VERIFY,
        ReshardState.CUTOVER,
        ReshardState.CLEANUP,
        ReshardState.DONE,
    ]


async def test_overlay_dual_writes_before_cutover_reads_source() -> None:
    mover = _seed_source({"r1": ("t", "p")})
    job = ReshardingJob(plan=_plan(_keys("t")), mover=mover)
    await job.begin_dual_write()
    overlay = job.overlay()
    key = ShardKey.of("t")
    # write hits both homes; read hits source.
    assert set(overlay.write_targets(key, "old")) == {"old", "new"}
    assert overlay.read_target(key, "old") == "old"


async def test_overlay_after_cutover_targets_new() -> None:
    mover = _seed_source({"r1": ("t", "p")})
    job = ReshardingJob(plan=_plan(_keys("t")), mover=mover)
    await job.begin_dual_write()
    await job.backfill()
    await job.verify()
    await job.cutover()
    overlay = job.overlay()
    key = ShardKey.of("t")
    assert overlay.read_target(key, "old") == "new"
    assert overlay.write_targets(key, "old") == ("new",)


async def test_backfill_is_batched() -> None:
    rows = {f"r{i}": ("t", f"p{i}") for i in range(25)}
    mover = _seed_source(rows)
    job = ReshardingJob(plan=_plan(_keys("t"), batch_size=10), mover=mover)
    await job.run()
    assert job.progress.rows_backfilled == 25
    assert len(mover.data["new"]["books"]) == 25


async def test_verify_mismatch_aborts_and_rolls_back() -> None:
    mover = _seed_source({"r1": ("t", "p1"), "r2": ("t", "p2")})
    job = ReshardingJob(plan=_plan(_keys("t")), mover=mover)
    await job.begin_dual_write()
    await job.backfill()
    # Corrupt the target so checksums diverge, then run the verify phase.
    mover.data["new"]["books"]["r1"] = (ShardKey.of("t"), "TAMPERED")
    with pytest.raises(ReshardError, match="verify mismatch"):
        await job.verify()
    # verify() raised but did not auto-abort (phase-level call); state is VERIFY.
    assert job.state is ReshardState.VERIFY
    # A manual abort (still pre-cutover) rolls the target rows back.
    await job.abort(job.progress.abort_reason or "verify failed")
    assert job.state is ReshardState.ABORTED
    assert mover.data["new"]["books"] == {}


class _DivergingMover(InMemoryReshardMover):
    """A mover whose copy silently corrupts the target so verify must fail."""

    async def copy_batch(  # type: ignore[override]
        self,
        source: str,
        target: str,
        table: str,
        keys: object,
        *,
        offset: int,
        limit: int,
    ) -> int:
        copied = await super().copy_batch(
            source, target, table, keys, offset=offset, limit=limit  # type: ignore[arg-type]
        )
        if self.data.get(target, {}).get(table):
            rid = next(iter(self.data[target][table]))
            self.data[target][table][rid] = (ShardKey.of("t"), "DIVERGED")
        return copied


async def test_verify_mismatch_via_run_path() -> None:
    seeded = _seed_source({"r1": ("t", "p1")})
    mover = _DivergingMover(data=seeded.data)
    job = ReshardingJob(plan=_plan(_keys("t")), mover=mover)

    with pytest.raises(ReshardError, match="verify mismatch"):
        await job.run()
    assert job.state is ReshardState.ABORTED
    # Rollback removed the (tampered) target rows.
    assert mover.data["new"]["books"] == {}


async def test_cannot_abort_after_cutover() -> None:
    mover = _seed_source({"r1": ("t", "p")})
    job = ReshardingJob(plan=_plan(_keys("t")), mover=mover)
    await job.begin_dual_write()
    await job.backfill()
    await job.verify()
    await job.cutover()
    with pytest.raises(ReshardError, match="cannot abort after cutover"):
        await job.abort("too late")


async def test_illegal_transition_rejected() -> None:
    mover = _seed_source({"r1": ("t", "p")})
    job = ReshardingJob(plan=_plan(_keys("t")), mover=mover)
    # Cannot backfill before dual-write.
    with pytest.raises(ReshardError, match="illegal transition"):
        await job.backfill()


async def test_overlay_publisher_invoked_on_each_transition() -> None:
    mover = _seed_source({"r1": ("t", "p")})
    published: list[MigrationOverlay] = []

    async def publish(overlay: MigrationOverlay) -> None:
        published.append(overlay)

    job = ReshardingJob(plan=_plan(_keys("t")), mover=mover, publish=publish)
    await job.run()
    # One publish per transition (DUAL_WRITE..DONE = 6 transitions).
    assert len(published) == 6
    # The DONE overlay is empty (migration retired).
    assert published[-1].is_empty()


async def test_plan_validation() -> None:
    with pytest.raises(ValueError, match="at least one key"):
        ReshardPlan(table="t", keys=(), source="a", target="b")
    with pytest.raises(ValueError, match="must differ"):
        ReshardPlan(table="t", keys=_keys("x"), source="a", target="a")
    with pytest.raises(ValueError, match="batch_size"):
        ReshardPlan(table="t", keys=_keys("x"), source="a", target="b", batch_size=0)


async def test_split_moves_subset_of_keys() -> None:
    # A "split" is modelled as moving a subset of keys to a new shard.
    mover = _seed_source(
        {
            "r1": ("k1", "a"),
            "r2": ("k2", "b"),
            "r3": ("k3", "c"),
            "r4": ("k4", "d"),
        }
    )
    # Move k3, k4 to 'new' (split the hot half off).
    job = ReshardingJob(plan=_plan(_keys("k3", "k4")), mover=mover)
    await job.run()
    assert set(mover.data["old"]["books"].keys()) == {"r1", "r2"}
    assert set(mover.data["new"]["books"].keys()) == {"r3", "r4"}
