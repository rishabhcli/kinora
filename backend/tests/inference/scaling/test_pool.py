"""Unit tests for the worker-pool state machine (app.inference.scaling.pool)."""

from __future__ import annotations

import pytest

from app.inference.scaling.instances import BillingModel, InstanceType
from app.inference.scaling.pool import Worker, WorkerPool, WorkerState


def _inst(**kw: object) -> InstanceType:
    base: dict[str, object] = {"name": "gpu-a10", "cost_per_hour": 3600.0, "cold_start_s": 10.0}
    base.update(kw)
    return InstanceType(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Worker lifecycle
# --------------------------------------------------------------------------- #


def test_worker_starts_warming_and_not_servable() -> None:
    w = Worker(worker_id=1, instance=_inst(), provisioned_at=0.0, ready_at=10.0)
    assert w.state is WorkerState.WARMING
    assert not w.is_servable
    assert w.free_slots == 0


def test_worker_becomes_warm_after_cold_start() -> None:
    w = Worker(worker_id=1, instance=_inst(), provisioned_at=0.0, ready_at=10.0)
    w.become_warm(10.0)
    assert w.state is WorkerState.WARM
    assert w.is_servable
    assert w.free_slots == 1


def test_worker_busy_when_serving_and_frees_on_finish() -> None:
    w = Worker(worker_id=1, instance=_inst(max_concurrency=2), provisioned_at=0.0, ready_at=0.0)
    w.become_warm(0.0)
    w.start_request(0.0)
    assert w.state is WorkerState.BUSY
    assert w.inflight == 1
    assert w.free_slots == 1  # concurrency 2
    w.start_request(0.0)
    assert w.free_slots == 0
    assert not w.is_servable
    w.finish_request(5.0)
    assert w.inflight == 1
    assert w.is_servable
    w.finish_request(10.0)
    assert w.state is WorkerState.WARM


def test_start_request_on_full_worker_raises() -> None:
    w = Worker(worker_id=1, instance=_inst(max_concurrency=1), provisioned_at=0.0, ready_at=0.0)
    w.become_warm(0.0)
    w.start_request(0.0)
    with pytest.raises(RuntimeError):
        w.start_request(0.0)


def test_finish_with_no_inflight_raises() -> None:
    w = Worker(worker_id=1, instance=_inst(), provisioned_at=0.0, ready_at=0.0)
    w.become_warm(0.0)
    with pytest.raises(RuntimeError):
        w.finish_request(1.0)


# --------------------------------------------------------------------------- #
# Cost accrual
# --------------------------------------------------------------------------- #


def test_per_second_billing_charges_for_existence() -> None:
    # cost_per_hour 3600 => 1.0/s.
    w = Worker(worker_id=1, instance=_inst(), provisioned_at=0.0, ready_at=10.0)
    w.accrue_to(100.0)
    assert w.accrued_cost == pytest.approx(100.0)
    # The first 10s were WARMING => cold-start cost.
    assert w.accrued_cold_start_cost > 0.0


def test_warming_slice_attributed_to_cold_start() -> None:
    w = Worker(worker_id=1, instance=_inst(cold_start_s=10.0), provisioned_at=0.0, ready_at=10.0)
    w.accrue_to(5.0)  # still warming
    assert w.accrued_cold_start_cost == pytest.approx(5.0)
    w.become_warm(10.0)  # charges 5..10 warming
    assert w.accrued_cold_start_cost == pytest.approx(10.0)
    w.accrue_to(30.0)  # 10..30 warm-idle
    assert w.accrued_idle_cost == pytest.approx(20.0)


def test_per_request_second_billing_only_charges_busy() -> None:
    inst = _inst(billing=BillingModel.PER_REQUEST_SECOND, max_concurrency=1)
    w = Worker(worker_id=1, instance=inst, provisioned_at=0.0, ready_at=0.0)
    w.become_warm(0.0)
    w.accrue_to(50.0)  # idle => no charge under FaaS billing
    assert w.accrued_cost == pytest.approx(0.0)
    w.start_request(50.0)
    w.accrue_to(60.0)  # busy 10s => charged
    assert w.accrued_cost == pytest.approx(10.0)


# --------------------------------------------------------------------------- #
# Pool
# --------------------------------------------------------------------------- #


def test_pool_launch_and_promote() -> None:
    pool = WorkerPool()
    w = pool.launch(instance=_inst(cold_start_s=10.0), now=0.0)
    assert pool.warming_count == 1
    assert pool.warm_count == 0
    assert w.ready_at == pytest.approx(10.0)
    pool.promote_ready(5.0)  # not ready yet
    assert pool.warm_count == 0
    pool.promote_ready(10.0)
    assert pool.warm_count == 1


def test_pool_pick_servable_spreads_load() -> None:
    pool = WorkerPool()
    a = pool.launch(instance=_inst(max_concurrency=2, cold_start_s=0.0), now=0.0)
    b = pool.launch(instance=_inst(max_concurrency=2, cold_start_s=0.0), now=0.0)
    pool.promote_ready(0.0)
    # Load one slot on a; the picker should prefer the emptier b next.
    a.start_request(0.0)
    chosen = pool.pick_servable()
    assert chosen is b


def test_pool_pick_servable_none_when_full() -> None:
    pool = WorkerPool()
    w = pool.launch(instance=_inst(max_concurrency=1, cold_start_s=0.0), now=0.0)
    pool.promote_ready(0.0)
    w.start_request(0.0)
    assert pool.pick_servable() is None
    assert pool.free_slots == 0


def test_pool_terminate_charges_and_removes() -> None:
    pool = WorkerPool()
    w = pool.launch(instance=_inst(), now=0.0)
    pool.terminate(w.worker_id, now=50.0)
    assert w.worker_id not in pool.workers
    assert pool.total_workers == 0


def test_pool_cost_rollups() -> None:
    pool = WorkerPool()
    pool.launch(instance=_inst(name="gpu-l20", cost_per_hour=3600.0, cold_start_s=5.0), now=0.0)
    pool.launch(instance=_inst(name="gpu-h20", cost_per_hour=7200.0, cold_start_s=5.0), now=0.0)
    total = pool.total_cost(100.0)
    by_type = pool.cost_by_instance_type(100.0)
    assert total == pytest.approx(by_type["gpu-l20"] + by_type["gpu-h20"])
    assert by_type["gpu-h20"] > by_type["gpu-l20"]  # dearer instance costs more
    assert pool.cold_start_cost(100.0) > 0.0
