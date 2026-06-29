"""Unit tests for the capacity planner config sweep (no infra)."""

from __future__ import annotations

import pytest

from app.mlplatform.serving.errors import ServingConfigError
from app.mlplatform.serving.model import ModelProfile
from app.mlplatform.serving.planner import CapacityPlan, CapacityPlanner, SweepGrid
from app.mlplatform.serving.requests import WorkloadGenerator
from app.mlplatform.serving.speculative import SpeculativeConfig


def _profile() -> ModelProfile:
    return ModelProfile(
        decode_ms_per_token=8.0,
        prefill_ms_per_token=0.8,
        kv_bytes_per_token=4096,
        params_billions=14.0,
        cost_per_1k_tokens=0.006,
    )


def _workload(n: int = 30) -> list:
    return WorkloadGenerator(
        seed="planner",
        n_requests=n,
        mean_prompt_tokens=200,
        prompt_spread=80,
        mean_gen_tokens=64,
        gen_spread=32,
        max_tokens=128,
    ).generate()


def _small_grid() -> SweepGrid:
    return SweepGrid(
        batch_sizes=(4, 8),
        token_budgets=(4096,),
        cache_blocks=(512,),
    )


def test_grid_validation() -> None:
    with pytest.raises(ServingConfigError):
        SweepGrid(batch_sizes=())


def test_sweep_produces_ranked_plan() -> None:
    planner = CapacityPlanner(_profile())
    plan = planner.sweep(_workload(), _small_grid(), objective="tokens_per_s")
    assert isinstance(plan, CapacityPlan)
    assert len(plan.candidates) == 2  # 2 batch sizes × 1 budget × 1 cache
    # Ranked best-first by throughput (descending).
    tps = [c.report.tokens_per_s for c in plan.candidates]
    assert tps == sorted(tps, reverse=True)
    assert plan.best().report.tokens_per_s == max(tps)


def test_sweep_objectives() -> None:
    planner = CapacityPlanner(_profile())
    work = _workload()
    grid = SweepGrid(batch_sizes=(2, 8), token_budgets=(4096,), cache_blocks=(512,))
    for objective in ("tokens_per_s", "p99_latency", "cost", "cost_per_token"):
        plan = planner.sweep(work, grid, objective=objective)
        assert plan.objective == objective
        assert len(plan.candidates) == 2


def test_sweep_unknown_objective_raises() -> None:
    planner = CapacityPlanner(_profile())
    with pytest.raises(ServingConfigError, match="unknown objective"):
        planner.sweep(_workload(), _small_grid(), objective="nope")


def test_sweep_skips_invalid_budget_vs_cache() -> None:
    # A token budget larger than the cache's token capacity must be skipped, not
    # raised. cache=64 blocks * 16 = 1024 tokens; budget 4096 is invalid and skipped,
    # budget 512 is valid → exactly the valid combos survive.
    planner = CapacityPlanner(_profile())
    grid = SweepGrid(batch_sizes=(4,), token_budgets=(512, 4096), cache_blocks=(64,))
    plan = planner.sweep(_workload(), grid)
    assert len(plan.candidates) == 1
    assert "budget=512" in plan.best().label


def test_sweep_all_invalid_raises() -> None:
    planner = CapacityPlanner(_profile())
    grid = SweepGrid(batch_sizes=(4,), token_budgets=(99999,), cache_blocks=(8,))
    with pytest.raises(ServingConfigError, match="no valid configs"):
        planner.sweep(_workload(), grid)


def test_sweep_is_deterministic() -> None:
    planner = CapacityPlanner(_profile())
    work = _workload()
    a = planner.sweep(work, _small_grid()).as_dict()
    b = planner.sweep(work, _small_grid()).as_dict()
    assert a == b


def test_speculative_and_prefix_dimensions_expand_grid() -> None:
    planner = CapacityPlanner(_profile())
    grid = SweepGrid(
        batch_sizes=(8,),
        token_budgets=(4096,),
        cache_blocks=(512,),
        prefix_keys=(None, "canon"),
        speculative=(
            SpeculativeConfig(enabled=False),
            SpeculativeConfig(enabled=True, k=4, alpha=0.8, draft_cost_ratio=0.05),
        ),
    )
    plan = planner.sweep(_workload(), grid)
    assert len(plan.candidates) == 4  # 2 prefix × 2 spec


def test_recommend_picks_cheapest_meeting_slo() -> None:
    planner = CapacityPlanner(_profile())
    work = _workload(20)
    grid = SweepGrid(batch_sizes=(4, 8, 16), token_budgets=(4096,), cache_blocks=(512,))
    # A generous SLO so several configs are feasible; the recommendation must be the
    # cheapest feasible one.
    rec = planner.recommend(work, p99_latency_slo_ms=1e9, grid=grid)
    plan = planner.sweep(work, grid, objective="cost")
    feasible = [c for c in plan.candidates if c.report.e2e.p99 <= 1e9]
    assert rec.report.total_cost == min(c.report.total_cost for c in feasible)


def test_recommend_falls_back_to_lowest_latency_when_slo_infeasible() -> None:
    planner = CapacityPlanner(_profile())
    work = _workload(40)
    grid = SweepGrid(batch_sizes=(2, 4), token_budgets=(4096,), cache_blocks=(512,))
    # An impossible SLO → fall back to the lowest-p99 candidate.
    rec = planner.recommend(work, p99_latency_slo_ms=0.0, grid=grid)
    plan = planner.sweep(work, grid)
    assert rec.report.e2e.p99 == min(c.report.e2e.p99 for c in plan.candidates)


def test_empty_plan_best_raises() -> None:
    plan = CapacityPlan(candidates=(), objective="cost")
    with pytest.raises(ServingConfigError):
        plan.best()
