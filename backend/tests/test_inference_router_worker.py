"""Tests for app.inference.router.worker — worker capacity + KV residency + pool."""

from __future__ import annotations

import pytest

from app.inference.router.errors import RouterConfigError
from app.inference.router.request import InferenceRequest
from app.inference.router.worker import Worker, WorkerConfig, WorkerPool


def _req(
    rid: str, *, prompt: int = 100, out: int = 0, prefix: str | None = None
) -> InferenceRequest:
    return InferenceRequest(
        request_id=rid, model="m", prompt_tokens=prompt, max_output_tokens=out, prefix_key=prefix
    )


def _worker(**kw: int) -> Worker:
    return Worker("w0", "m", WorkerConfig(**kw))


def test_admit_reserves_tokens_and_slots() -> None:
    w = _worker(token_capacity=1000, max_slots=4)
    w.admit(_req("a", prompt=300))
    assert w.tokens_in_use == 300
    assert w.slots_in_use == 1
    assert w.token_headroom == 700
    assert w.slot_headroom == 3


def test_can_admit_respects_token_capacity() -> None:
    w = _worker(token_capacity=100, max_slots=10)
    assert w.can_admit(_req("a", prompt=100)) is True
    assert w.can_admit(_req("b", prompt=101)) is False


def test_can_admit_respects_slot_capacity() -> None:
    w = _worker(token_capacity=10_000, max_slots=1)
    w.admit(_req("a", prompt=1))
    assert w.can_admit(_req("b", prompt=1)) is False


def test_admitting_when_full_raises() -> None:
    w = _worker(token_capacity=100, max_slots=1)
    w.admit(_req("a", prompt=100))
    with pytest.raises(RouterConfigError):
        w.admit(_req("b", prompt=1))


def test_complete_releases_capacity() -> None:
    w = _worker(token_capacity=1000, max_slots=4)
    r = _req("a", prompt=300)
    w.admit(r)
    w.complete(r)
    assert w.tokens_in_use == 0
    assert w.slots_in_use == 0


def test_complete_with_actual_tokens_releases_actual() -> None:
    w = _worker(token_capacity=1000, max_slots=4)
    r = _req("a", prompt=200, out=300)  # reserved 500
    w.admit(r)
    w.complete(r, actual_total_tokens=250)  # generation finished short
    assert w.tokens_in_use == 250


def test_complete_clamps_at_zero() -> None:
    w = _worker(token_capacity=1000, max_slots=4)
    r = _req("a", prompt=100)
    w.admit(r)
    w.complete(r, actual_total_tokens=99999)  # absurd over-release
    assert w.tokens_in_use == 0
    assert w.slots_in_use == 0


def test_prefix_residency_lru_eviction() -> None:
    w = _worker(token_capacity=10_000, max_slots=10, prefix_capacity=2)
    w.touch_prefix("p1")
    w.touch_prefix("p2")
    assert w.has_prefix("p1") and w.has_prefix("p2")
    w.touch_prefix("p3")  # evicts the LRU (p1)
    assert not w.has_prefix("p1")
    assert w.has_prefix("p2") and w.has_prefix("p3")


def test_touch_prefix_bumps_recency() -> None:
    w = _worker(token_capacity=10_000, max_slots=10, prefix_capacity=2)
    w.touch_prefix("p1")
    w.touch_prefix("p2")
    w.touch_prefix("p1")  # p1 now MRU
    w.touch_prefix("p3")  # evicts p2, not p1
    assert w.has_prefix("p1") and w.has_prefix("p3")
    assert not w.has_prefix("p2")


def test_admit_marks_prefix_resident() -> None:
    w = _worker(token_capacity=10_000, max_slots=10)
    w.admit(_req("a", prompt=10, prefix="warm"))
    assert w.has_prefix("warm")


def test_unhealthy_worker_not_schedulable() -> None:
    w = _worker()
    w.set_healthy(False)
    assert not w.schedulable
    assert not w.can_admit(_req("a"))


def test_draining_worker_not_schedulable() -> None:
    w = _worker()
    w.set_draining(True)
    assert not w.schedulable


def test_utilization_is_max_of_token_and_slot() -> None:
    w = _worker(token_capacity=1000, max_slots=2)
    w.admit(_req("a", prompt=100))  # 10% tokens, 50% slots
    assert w.utilization == pytest.approx(0.5)


def test_worker_config_validation() -> None:
    with pytest.raises(RouterConfigError):
        WorkerConfig(token_capacity=0)
    with pytest.raises(RouterConfigError):
        WorkerConfig(max_slots=0)
    with pytest.raises(RouterConfigError):
        WorkerConfig(prefix_capacity=0)


def test_worker_id_and_model_required() -> None:
    with pytest.raises(RouterConfigError):
        Worker("", "m")
    with pytest.raises(RouterConfigError):
        Worker("w", "")


# -- pool ----------------------------------------------------------------- #


def test_pool_rejects_model_mismatch() -> None:
    with pytest.raises(RouterConfigError):
        WorkerPool("m", [Worker("w", "other")])


def test_pool_rejects_duplicate_ids() -> None:
    pool = WorkerPool("m")
    pool.add_worker("w0")
    with pytest.raises(RouterConfigError):
        pool.add_worker("w0")


def test_pool_schedulable_excludes_draining_and_unhealthy() -> None:
    pool = WorkerPool("m")
    w0 = pool.add_configured_worker("w0", WorkerConfig())
    w1 = pool.add_configured_worker("w1", WorkerConfig())
    pool.add_configured_worker("w2", WorkerConfig())
    w0.set_draining(True)
    w1.set_healthy(False)
    ids = {w.worker_id for w in pool.schedulable_workers()}
    assert ids == {"w2"}


def test_pool_drain_then_remove() -> None:
    pool = WorkerPool("m")
    pool.add_worker("w0")
    pool.drain_worker("w0")
    assert pool.get("w0").draining is True  # type: ignore[union-attr]
    pool.remove_worker("w0")
    assert pool.get("w0") is None


def test_pool_capacity_aggregates_schedulable_only() -> None:
    pool = WorkerPool("m")
    pool.add_configured_worker("w0", WorkerConfig(token_capacity=1000))
    w1 = pool.add_configured_worker("w1", WorkerConfig(token_capacity=2000))
    assert pool.total_token_capacity == 3000
    w1.set_draining(True)
    assert pool.total_token_capacity == 1000


def test_pool_snapshot_returns_views() -> None:
    pool = WorkerPool("m")
    pool.add_configured_worker("w0", WorkerConfig(token_capacity=500, max_slots=4))
    views = pool.snapshot()
    assert len(views) == 1
    assert views[0].worker_id == "w0"
    assert views[0].token_capacity == 500
    assert views[0].healthy is True
