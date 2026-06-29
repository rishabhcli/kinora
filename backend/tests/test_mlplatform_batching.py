"""Unit + property tests for the continuous-batching scheduler (no infra)."""

from __future__ import annotations

import pytest

from app.mlplatform.serving.batching import (
    AdmissionDecision,
    BatchScheduler,
    ContinuousBatchConfig,
)
from app.mlplatform.serving.errors import InvariantViolationError, ServingConfigError
from app.mlplatform.serving.kvcache import PagedKVCache, PagedKVConfig
from app.mlplatform.serving.requests import InferenceRequest, RequestState, WorkloadGenerator


def _sched(
    *,
    max_batch_size: int = 4,
    max_batch_tokens: int = 1024,
    max_admit_per_step: int = 8,
    total_blocks: int = 256,
    block_tokens: int = 16,
) -> BatchScheduler:
    cfg = ContinuousBatchConfig(
        max_batch_size=max_batch_size,
        max_batch_tokens=max_batch_tokens,
        max_admit_per_step=max_admit_per_step,
    )
    cache = PagedKVCache(PagedKVConfig(total_blocks=total_blocks, block_tokens=block_tokens))
    return BatchScheduler(cfg, cache)


def _req(rid: str, *, arrival: float = 0.0, prompt: int = 64, prio: int = 0) -> InferenceRequest:
    return InferenceRequest(
        rid, arrival, prompt_tokens=prompt, max_tokens=32, gen_tokens=16, priority=prio
    )


def test_config_validation() -> None:
    with pytest.raises(ServingConfigError):
        ContinuousBatchConfig(max_batch_size=0)
    with pytest.raises(ServingConfigError):
        ContinuousBatchConfig(max_batch_tokens=0)
    with pytest.raises(ServingConfigError):
        ContinuousBatchConfig(max_admit_per_step=0)


def test_enqueue_requires_queued_state() -> None:
    s = _sched()
    r = _req("r")
    r.state = RequestState.DECODING
    with pytest.raises(InvariantViolationError):
        s.enqueue(r)


def test_schedule_respects_max_batch_size() -> None:
    s = _sched(max_batch_size=2)
    for i in range(5):
        s.enqueue(_req(f"r{i}"))
    decision = s.schedule(0.0)
    assert len(decision.admit) == 2
    s.apply(decision, 0.0)
    assert s.n_running == 2
    assert s.n_waiting == 3


def test_schedule_respects_token_budget() -> None:
    # The budget bounds the worst-case reservation (prompt + target_tokens, where
    # target = min(max_tokens, gen_tokens)). With prompt=64 and target=16 each
    # request reserves 80 tokens, so a 200-token budget fits exactly two (160) and
    # the third (240) overflows.
    s = _sched(max_batch_size=10, max_batch_tokens=200, block_tokens=16, total_blocks=256)
    for i in range(5):
        s.enqueue(_req(f"r{i}", prompt=64))  # reserves 64 + 16 = 80 each
    decision = s.schedule(0.0)
    assert len(decision.admit) == 2


def test_schedule_respects_admit_cap() -> None:
    s = _sched(
        max_batch_size=100, max_batch_tokens=100000, max_admit_per_step=3, total_blocks=10000
    )
    for i in range(10):
        s.enqueue(_req(f"r{i}", prompt=16))
    decision = s.schedule(0.0)
    assert len(decision.admit) == 3


def test_apply_moves_to_prefill_and_reserves_cache() -> None:
    s = _sched()
    s.enqueue(_req("r0", prompt=64))
    decision = s.schedule(5.0)
    s.apply(decision, 5.0)
    run = s.running[0]
    assert run.state == RequestState.PREFILL
    assert run.admit_ms == 5.0
    assert s.cache.blocks_held("r0") == 4  # 64 / 16


def test_priority_ordering_in_admission() -> None:
    s = _sched(max_batch_size=1)
    s.enqueue(_req("low", arrival=0.0, prio=5))
    s.enqueue(_req("high", arrival=10.0, prio=0))
    decision = s.schedule(20.0)
    # The higher-priority (lower number) request is admitted first despite arriving later.
    assert [r.request_id for r in decision.admit] == ["high"]


def test_complete_frees_cache() -> None:
    s = _sched()
    s.enqueue(_req("r0"))
    s.apply(s.schedule(0.0), 0.0)
    assert s.cache.used_blocks > 0
    s.complete("r0")
    assert s.n_running == 0
    assert s.cache.used_blocks == 0


def test_complete_unknown_raises() -> None:
    s = _sched()
    with pytest.raises(InvariantViolationError):
        s.complete("ghost")


def test_preempt_returns_to_queue_and_frees_cache() -> None:
    s = _sched()
    s.enqueue(_req("r0"))
    s.apply(s.schedule(0.0), 0.0)
    r = s.running[0]
    r.generated = 5
    s.preempt("r0")
    assert s.n_running == 0
    assert s.n_waiting == 1
    requeued = s.waiting[0]
    assert requeued.state == RequestState.QUEUED
    assert requeued.generated == 0  # recompute on re-admission
    assert s.cache.used_blocks == 0


def test_victim_for_preemption_picks_lowest_priority_newest() -> None:
    s = _sched(max_batch_size=3, max_batch_tokens=100000, total_blocks=10000)
    s.enqueue(_req("old-hi", arrival=0.0, prio=0))
    s.enqueue(_req("new-lo", arrival=0.0, prio=5))
    s.apply(s.schedule(0.0), 0.0)
    s.apply(s.schedule(1.0), 1.0)
    victim = s.victim_for_preemption()
    assert victim is not None
    assert victim.request_id == "new-lo"


def test_idle_state() -> None:
    s = _sched()
    assert s.is_idle
    s.enqueue(_req("r0"))
    assert not s.is_idle


# -- property sweeps ------------------------------------------------------- #


@pytest.mark.parametrize("seed_i", range(30))
def test_property_admission_never_breaks_limits(seed_i: int) -> None:
    """For any seeded workload + step sequence, the running batch never exceeds the
    size or token limits, and the cache is never overcommitted."""
    s = _sched(
        max_batch_size=4 + seed_i % 5,
        max_batch_tokens=512 + 64 * (seed_i % 4),
        total_blocks=512,
        block_tokens=16,
    )
    reqs = WorkloadGenerator(
        seed=f"batch-{seed_i}",
        n_requests=40,
        mean_prompt_tokens=80,
        prompt_spread=40,
        max_tokens=64,
    ).generate()
    pending = list(reqs)
    clock = 0.0
    while pending or not s.is_idle:
        clock += 10.0
        # Enqueue arrivals up to clock.
        while pending and pending[0].arrival_ms <= clock:
            s.enqueue(pending.pop(0))
        decision = s.schedule(clock)
        s.apply(decision, clock)
        # Invariants must hold after every apply.
        s.assert_invariants()
        assert s.n_running <= s.config.max_batch_size
        # The worst-case reservation — the quantity the budget actually bounds —
        # never exceeds the budget; the live total is dominated by it.
        assert s.reserved_token_total() <= s.config.max_batch_tokens
        assert s.live_token_total() <= s.config.max_batch_tokens
        assert s.cache.used_blocks <= s.cache.capacity
        # Drain one running request per step so the loop terminates.
        if s.running:
            s.complete(s.running[0].request_id)
    assert s.is_idle


def test_reserved_token_total_bounds_live_total() -> None:
    s = _sched(max_batch_size=4, max_batch_tokens=100000, total_blocks=10000)
    r = InferenceRequest("r", 0.0, prompt_tokens=40, max_tokens=20, gen_tokens=20)
    s.enqueue(r)
    s.apply(s.schedule(0.0), 0.0)
    # At admission, reserved = prompt + target = 60; live = prompt only = 40.
    assert s.reserved_token_total() == 60
    assert s.live_token_total() == 40
    assert s.live_token_total() <= s.reserved_token_total()


def test_oversized_request_runs_solo_and_never_starves() -> None:
    # A request whose worst-case reservation exceeds the whole budget can only run
    # alone; the scheduler must still admit it (when the batch is empty) so it does
    # not starve forever.
    s = _sched(max_batch_size=8, max_batch_tokens=64, total_blocks=10000, block_tokens=16)
    big = InferenceRequest("big", 0.0, prompt_tokens=200, max_tokens=100, gen_tokens=100)
    small = InferenceRequest("small", 0.0, prompt_tokens=16, max_tokens=8, gen_tokens=8)
    s.enqueue(big)
    s.enqueue(small)
    decision = s.schedule(0.0)
    # The oversized head is admitted solo; the small one waits behind it (FIFO).
    assert [r.request_id for r in decision.admit] == ["big"]
    s.apply(decision, 0.0)
    assert s.n_running == 1
    # It must not be blocked by the budget invariant — running solo is allowed.
    s.assert_invariants()


def test_admission_decision_is_immutable_value() -> None:
    d = AdmissionDecision(admit=(), preempt=())
    assert d.admit == ()
    assert d.preempt == ()
