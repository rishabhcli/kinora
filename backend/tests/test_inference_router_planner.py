"""Tests for app.inference.router.planner — offline batch planning (§11.1)."""

from __future__ import annotations

import pytest

from app.inference.router.errors import RouterConfigError
from app.inference.router.planner import BatchPlanner, PlannerConfig
from app.inference.router.request import InferenceRequest


def _req(rid: str, *, prompt: int = 100, prefix: str | None = None) -> InferenceRequest:
    return InferenceRequest(
        request_id=rid, model="m", prompt_tokens=prompt, max_output_tokens=0, prefix_key=prefix
    )


def test_packs_into_token_budget_batches() -> None:
    planner = BatchPlanner(PlannerConfig(token_budget=300, slot_budget=100, group_by_prefix=False))
    reqs = [_req(f"r{i}", prompt=100) for i in range(7)]
    plan = planner.plan(reqs)
    # 3 per batch (300/100) -> 3 batches (3,3,1).
    assert plan.batch_count == 3
    assert [len(b) for b in plan.batches] == [3, 3, 1]
    assert plan.total_packed == 7


def test_slot_budget_caps_batch_size() -> None:
    planner = BatchPlanner(
        PlannerConfig(token_budget=100_000, slot_budget=2, group_by_prefix=False)
    )
    plan = planner.plan([_req(f"r{i}", prompt=1) for i in range(5)])
    assert [len(b) for b in plan.batches] == [2, 2, 1]


def test_oversized_request_reported() -> None:
    planner = BatchPlanner(PlannerConfig(token_budget=100, slot_budget=10, group_by_prefix=False))
    plan = planner.plan([_req("ok", prompt=50), _req("huge", prompt=500)])
    assert [r.request_id for r in plan.oversized] == ["huge"]
    assert plan.total_packed == 1


def test_group_by_prefix_keeps_warm_batches() -> None:
    planner = BatchPlanner(
        PlannerConfig(token_budget=10_000, slot_budget=100, group_by_prefix=True)
    )
    reqs = [
        _req("a1", prefix="A"),
        _req("b1", prefix="B"),
        _req("a2", prefix="A"),
        _req("b2", prefix="B"),
    ]
    plan = planner.plan(reqs)
    # Each prefix forms its own (warm) batch, in first-seen order.
    assert plan.batch_count == 2
    assert {r.request_id for r in plan.batches[0]} == {"a1", "a2"}
    assert {r.request_id for r in plan.batches[1]} == {"b1", "b2"}


def test_fill_ratios_report_planning_quality() -> None:
    planner = BatchPlanner(PlannerConfig(token_budget=400, slot_budget=100, group_by_prefix=False))
    plan = planner.plan([_req(f"r{i}", prompt=100) for i in range(4)])
    ratios = plan.fill_ratios(400)
    # One full batch (4*100=400 == budget) -> 1.0 fill.
    assert ratios == [pytest.approx(1.0)]


def test_empty_workload() -> None:
    planner = BatchPlanner()
    plan = planner.plan([])
    assert plan.batch_count == 0 and not plan.oversized


def test_config_validation() -> None:
    with pytest.raises(RouterConfigError):
        PlannerConfig(token_budget=0)
    with pytest.raises(RouterConfigError):
        PlannerConfig(slot_budget=0)


def test_all_requests_accounted_for() -> None:
    planner = BatchPlanner(PlannerConfig(token_budget=250, slot_budget=3))
    reqs = [_req(f"r{i}", prompt=100, prefix=f"p{i % 2}") for i in range(10)]
    reqs.append(_req("big", prompt=9999))
    plan = planner.plan(reqs)
    assert plan.total_packed + len(plan.oversized) == len(reqs)
