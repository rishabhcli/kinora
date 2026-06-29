"""Unit + property tests for the discrete-event serving simulator (no infra).

These tests are the heart of the facet's correctness story: the brief requires the
serving scheduler simulation to be property-tested for invariants —

* no request starves (every admitted request eventually completes),
* batch limits are respected at every step,
* the KV-cache is never overcommitted and fully drains,
* and the whole simulation is deterministic.

We assert those across a wide sweep of seeded workloads + configurations.
"""

from __future__ import annotations

import pytest

from app.mlplatform.serving.batching import ContinuousBatchConfig
from app.mlplatform.serving.errors import ServingConfigError
from app.mlplatform.serving.kvcache import PagedKVConfig
from app.mlplatform.serving.model import ModelProfile
from app.mlplatform.serving.requests import InferenceRequest, RequestState, WorkloadGenerator
from app.mlplatform.serving.simulator import ServingSimulator, SimConfig
from app.mlplatform.serving.speculative import SpeculativeConfig


def _profile() -> ModelProfile:
    return ModelProfile(
        decode_ms_per_token=5.0,
        prefill_ms_per_token=0.5,
        kv_bytes_per_token=2048,
        params_billions=7.0,
        cost_per_1k_tokens=0.002,
    )


def _config(**over: object) -> SimConfig:
    base: dict[str, object] = {
        "profile": _profile(),
        "cache": PagedKVConfig(total_blocks=512, block_tokens=16),
        "batch": ContinuousBatchConfig(
            max_batch_size=8, max_batch_tokens=4096, max_admit_per_step=4
        ),
    }
    base.update(over)
    return SimConfig(**base)  # type: ignore[arg-type]


def _workload(seed: str, n: int = 40, **over: object) -> list[InferenceRequest]:
    kw: dict[str, object] = {
        "seed": seed,
        "n_requests": n,
        "mean_prompt_tokens": 200,
        "prompt_spread": 80,
        "mean_gen_tokens": 64,
        "gen_spread": 32,
        "max_tokens": 128,
    }
    kw.update(over)
    return WorkloadGenerator(**kw).generate()  # type: ignore[arg-type]


# -- config validation ----------------------------------------------------- #


def test_simconfig_validation() -> None:
    with pytest.raises(ServingConfigError):
        _config(max_steps=0)
    with pytest.raises(ServingConfigError):
        _config(tokens_per_step_plain=0)


def test_simconfig_rejects_batch_budget_above_cache_capacity() -> None:
    # cache holds 16 blocks * 16 tokens = 256; a 512-token batch budget is illegal.
    with pytest.raises(ServingConfigError, match="exceeds KV-cache"):
        SimConfig(
            profile=_profile(),
            cache=PagedKVConfig(total_blocks=16, block_tokens=16),
            batch=ContinuousBatchConfig(max_batch_tokens=512, max_batch_size=4),
        )


# -- basic end-to-end ------------------------------------------------------ #


def test_run_completes_all_requests() -> None:
    sim = ServingSimulator(_config())
    reqs = _workload("e2e")
    report = sim.run(reqs)
    assert report.n_completed == len(reqs)
    assert report.n_failed == 0
    assert report.wall_clock_ms > 0


def test_run_does_not_mutate_caller_requests() -> None:
    sim = ServingSimulator(_config())
    reqs = _workload("nomutate")
    sim.run(reqs)
    assert all(r.state == RequestState.QUEUED for r in reqs)
    assert all(r.generated == 0 for r in reqs)
    assert all(r.finish_ms is None for r in reqs)


def test_run_is_deterministic() -> None:
    sim = ServingSimulator(_config())
    reqs = _workload("det")
    assert sim.run(reqs).as_dict() == sim.run(reqs).as_dict()


def test_two_simulators_same_config_agree() -> None:
    reqs = _workload("agree")
    a = ServingSimulator(_config()).run(reqs).as_dict()
    b = ServingSimulator(_config()).run(reqs).as_dict()
    assert a == b


def test_empty_workload() -> None:
    report = ServingSimulator(_config()).run([])
    assert report.n_completed == 0
    assert report.wall_clock_ms == 0.0


def test_ttft_le_e2e() -> None:
    report = ServingSimulator(_config()).run(_workload("ttft"))
    assert report.ttft.mean <= report.e2e.mean
    assert report.ttft.max <= report.e2e.max


# -- feature paths --------------------------------------------------------- #


def test_prefix_reuse_increases_reuse_ratio_without_changing_correctness() -> None:
    reqs = _workload("prefix")
    plain = ServingSimulator(_config()).run(reqs)
    reuse = ServingSimulator(_config(shared_prefix_key="canon-slice")).run(reqs)
    assert plain.kv_reuse_ratio == 0.0
    assert reuse.kv_reuse_ratio > 0.5
    # Both still complete every request.
    assert reuse.n_completed == plain.n_completed == len(reqs)


def test_speculative_decoding_speeds_up_and_reports_speedup() -> None:
    reqs = _workload("spec")
    plain = ServingSimulator(_config()).run(reqs)
    spec = ServingSimulator(
        _config(speculative=SpeculativeConfig(enabled=True, k=4, alpha=0.85, draft_cost_ratio=0.05))
    ).run(reqs)
    assert plain.speculative_speedup == 1.0
    assert spec.speculative_speedup > 1.5
    assert spec.wall_clock_ms < plain.wall_clock_ms
    assert spec.n_completed == len(reqs)


def test_higher_throughput_with_more_workers_proxy() -> None:
    """A larger batch budget should not reduce completion and should not increase
    wall-clock (more parallelism is never worse)."""
    reqs = _workload("scale", n=60)
    small = ServingSimulator(
        _config(
            batch=ContinuousBatchConfig(
                max_batch_size=2, max_batch_tokens=2048, max_admit_per_step=2
            )
        )
    ).run(reqs)
    big = ServingSimulator(
        _config(
            batch=ContinuousBatchConfig(
                max_batch_size=16, max_batch_tokens=4096, max_admit_per_step=8
            )
        )
    ).run(reqs)
    assert big.n_completed == small.n_completed == 60
    assert big.wall_clock_ms <= small.wall_clock_ms


# -- preemption under cache pressure --------------------------------------- #


def test_tight_cache_still_completes_everything_via_preemption() -> None:
    """A deliberately tiny KV-cache forces preemption; no request may starve."""
    cfg = SimConfig(
        profile=_profile(),
        # Very small pool: a few sequences at most.
        cache=PagedKVConfig(total_blocks=24, block_tokens=16),
        batch=ContinuousBatchConfig(max_batch_size=8, max_batch_tokens=256, max_admit_per_step=4),
    )
    reqs = _workload("tight", n=30, mean_prompt_tokens=64, prompt_spread=16, max_tokens=96)
    report = ServingSimulator(cfg).run(reqs)
    assert report.n_completed == len(reqs)  # nobody starved despite preemption


# -- property sweeps: the named invariants --------------------------------- #


@pytest.mark.parametrize("seed_i", range(40))
def test_property_all_requests_complete_no_starvation(seed_i: int) -> None:
    """For any seeded workload + config, every request completes (no starvation),
    batch limits hold, and the cache drains — these are checked inside the
    simulator's own invariant guards too, so a violation would raise."""
    cfg = SimConfig(
        profile=_profile(),
        cache=PagedKVConfig(total_blocks=64 + 32 * (seed_i % 6), block_tokens=16),
        batch=ContinuousBatchConfig(
            max_batch_size=2 + seed_i % 7,
            max_batch_tokens=256 + 256 * (seed_i % 4),
            max_admit_per_step=1 + seed_i % 4,
        ),
        speculative=SpeculativeConfig(
            enabled=bool(seed_i % 2),
            k=2 + seed_i % 3,
            alpha=0.5 + 0.1 * (seed_i % 4),
            draft_cost_ratio=0.05,
        ),
        shared_prefix_key="canon" if seed_i % 3 == 0 else None,
    )
    reqs = _workload(
        f"prop-{seed_i}",
        n=20 + seed_i % 30,
        mean_prompt_tokens=80,
        prompt_spread=40,
        mean_gen_tokens=48,
        gen_spread=24,
        max_tokens=96,
        priority_levels=1 + seed_i % 3,
    )
    report = ServingSimulator(cfg).run(reqs)
    # No starvation: completions == arrivals.
    assert report.n_completed == len(reqs)
    assert report.n_failed == 0
    # Batch-size invariant surfaced through telemetry.
    assert report.peak_batch_occupancy <= cfg.batch.max_batch_size
    # KV utilization never exceeds 1.0 (cache never overcommitted).
    assert report.peak_kv_utilization <= 1.0 + 1e-9
    # Determinism within the sweep.
    assert ServingSimulator(cfg).run(reqs).as_dict() == report.as_dict()


@pytest.mark.parametrize("seed_i", range(15))
def test_property_token_conservation(seed_i: int) -> None:
    """Total generated tokens equal the sum of each request's target tokens — every
    request emits exactly what it was supposed to, no more, no less."""
    cfg = _config(
        batch=ContinuousBatchConfig(
            max_batch_size=2 + seed_i % 5, max_batch_tokens=2048, max_admit_per_step=4
        )
    )
    reqs = _workload(f"cons-{seed_i}", n=25, max_tokens=96)
    expected = sum(r.target_tokens for r in reqs)
    report = ServingSimulator(cfg).run(reqs)
    assert report.total_generated_tokens == expected
