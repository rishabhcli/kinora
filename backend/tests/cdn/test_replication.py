"""Replication: idempotency, checksum verification, lag tracking, reconcile."""

from __future__ import annotations

import pytest

from app.cdn.errors import OriginMissingObjectError, UnknownRegionError
from app.cdn.replication import ReplicaStatus, ReplicationManager
from app.cdn.testing import FakeClock, FakeRegionStore, demo_topology
from app.media.hashing import sha256_hex

KEY = "clips/book1/shot_00001.mp4"
DATA = b"a finished clip, persisted to origin" * 8


def _manager(
    *, clock: FakeClock | None = None
) -> tuple[ReplicationManager, dict[str, FakeRegionStore], FakeClock]:
    topo = demo_topology()
    stores = {rid: FakeRegionStore(rid) for rid in topo.region_ids}
    clk = clock or FakeClock()
    mgr = ReplicationManager(topology=topo, stores=stores, clock=clk)
    return mgr, stores, clk


async def test_replicate_copies_to_all_replicas_checksum_verified() -> None:
    mgr, stores, _ = _manager()
    stores["na"].seed(KEY, DATA)

    report = await mgr.replicate(KEY)

    assert report.fully_replicated
    assert report.checksum == sha256_hex(DATA)
    assert {r.region_id for r in report.results} == {"eu", "ap"}
    for r in report.results:
        assert r.status is ReplicaStatus.REPLICATED
    # Bytes are byte-identical in every replica.
    assert stores["eu"].digest(KEY) == sha256_hex(DATA)
    assert stores["ap"].digest(KEY) == sha256_hex(DATA)


async def test_replicate_is_idempotent_second_run_skips() -> None:
    mgr, stores, _ = _manager()
    stores["na"].seed(KEY, DATA)

    await mgr.replicate(KEY)
    puts_after_first = stores["eu"].puts
    report2 = await mgr.replicate(KEY)

    assert all(r.status is ReplicaStatus.SKIPPED for r in report2.results)
    # No extra write happened on the idempotent re-run.
    assert stores["eu"].puts == puts_after_first


async def test_origin_missing_raises() -> None:
    mgr, _, _ = _manager()
    with pytest.raises(OriginMissingObjectError):
        await mgr.replicate(KEY)


async def test_unknown_target_raises() -> None:
    mgr, stores, _ = _manager()
    stores["na"].seed(KEY, DATA)
    with pytest.raises(UnknownRegionError):
        await mgr.replicate(KEY, targets=["mars"])


async def test_replication_lag_tracked_against_origin_write() -> None:
    clk = FakeClock(start=1000.0)
    mgr, stores, _ = _manager(clock=clk)
    stores["na"].seed(KEY, DATA)

    # Origin wrote at t=1000; replication happens 30s later.
    clk.advance(30.0)
    await mgr.replicate(KEY, origin_written_at=1000.0)

    lag = await mgr.replica_lag_s("eu", KEY)
    assert lag == pytest.approx(30.0)
    # An unreplicated region has no lag reading.
    assert await mgr.replica_lag_s("eu", "clips/book1/other.mp4") is None


async def test_reconcile_repairs_a_dropped_replica() -> None:
    mgr, stores, _ = _manager()
    stores["na"].seed(KEY, DATA)
    await mgr.replicate(KEY)

    # Simulate the object being lost on EU while NA/AP keep it (missed object).
    stores["eu"].drop(KEY)
    assert not await stores["eu"].exists(KEY)

    reports = await mgr.reconcile([KEY])

    assert len(reports) == 1
    by_region = {r.region_id: r for r in reports[0].results}
    assert by_region["eu"].status is ReplicaStatus.REPAIRED  # was missing -> repaired
    assert by_region["ap"].status is ReplicaStatus.SKIPPED  # already fine
    assert stores["eu"].digest(KEY) == sha256_hex(DATA)


async def test_reconcile_repairs_a_divergent_replica() -> None:
    mgr, stores, _ = _manager()
    stores["na"].seed(KEY, DATA)
    await mgr.replicate(KEY)

    # Bit-rot / tamper: EU bytes drift from origin.
    stores["eu"].corrupt(KEY, b"corrupted bytes that do not match")
    assert stores["eu"].digest(KEY) != sha256_hex(DATA)

    reports = await mgr.reconcile([KEY])
    by_region = {r.region_id: r for r in reports[0].results}
    assert by_region["eu"].status is ReplicaStatus.REPAIRED
    # And the repair restored the canonical bytes.
    assert stores["eu"].digest(KEY) == sha256_hex(DATA)


async def test_reconcile_skips_origin_gced_key_without_erroring() -> None:
    mgr, _, _ = _manager()
    # Origin never had this key (e.g. GC'd) — the sweep logs & continues.
    reports = await mgr.reconcile(["clips/book1/ghost.mp4"])
    assert reports == []


async def test_readback_checksum_mismatch_marks_replica_failed() -> None:
    mgr, stores, _ = _manager()
    stores["na"].seed(KEY, DATA)
    # EU silently corrupts on write -> read-back digest won't match origin's.
    stores["eu"].corrupt_on_put = b"silently wrong bytes"

    report = await mgr.replicate(KEY)

    assert not report.fully_replicated
    by_region = {r.region_id: r for r in report.results}
    assert by_region["eu"].status is ReplicaStatus.FAILED
    assert by_region["eu"].detail is not None
    assert by_region["ap"].status is ReplicaStatus.REPLICATED
    # A failed replica is not recorded in the ledger as caught-up.
    assert await mgr.replica_lag_s("eu", KEY) is None
    assert len(report.failures()) == 1


async def test_divergent_replica_triggers_repair_on_plain_replicate() -> None:
    mgr, stores, _ = _manager()
    stores["na"].seed(KEY, DATA)
    await mgr.replicate(KEY)
    stores["eu"].corrupt(KEY, b"divergent")

    report = await mgr.replicate(KEY)
    by_region = {r.region_id: r for r in report.results}
    # EU diverged -> re-written as a repair; AP unchanged -> skipped.
    assert by_region["eu"].status is ReplicaStatus.REPAIRED
    assert by_region["ap"].status is ReplicaStatus.SKIPPED
