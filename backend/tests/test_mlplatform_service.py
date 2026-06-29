"""Unit tests for the serving-platform façade + default catalog (no infra)."""

from __future__ import annotations

import pytest

from app.mlplatform.serving.catalog import DEFAULT_CATALOG, build_default_registry
from app.mlplatform.serving.distillation import DistillationSpec
from app.mlplatform.serving.errors import ModelNotFoundError
from app.mlplatform.serving.model import ModelKind, Stage
from app.mlplatform.serving.registry import EvalGate
from app.mlplatform.serving.service import ServingPlatform

# -- catalog --------------------------------------------------------------- #


def test_default_catalog_has_each_model_kind() -> None:
    kinds = {v.kind for v in DEFAULT_CATALOG}
    assert ModelKind.REASONING in kinds
    assert ModelKind.JUDGE in kinds
    assert ModelKind.REWARD in kinds
    assert ModelKind.DRAFT in kinds


def test_build_default_registry_registers_all_in_dev() -> None:
    reg = build_default_registry()
    for v in DEFAULT_CATALOG:
        stored = reg.get(v.ref)
        assert stored.stage == Stage.DEV


def test_draft_model_has_accept_rate() -> None:
    draft = next(v for v in DEFAULT_CATALOG if v.kind == ModelKind.DRAFT)
    assert draft.profile.accept_rate > 0.0


# -- platform construction ------------------------------------------------- #


def test_platform_defaults_are_offline() -> None:
    p = ServingPlatform()
    assert p.dataset("eval-default") is not None
    assert p.registry.exists("kinora-brain@1.0.0")


def test_dataset_lookup() -> None:
    p = ServingPlatform()
    ds = p.dataset("distill-default")
    assert len(ds) == 64


# -- distillation through the façade --------------------------------------- #


def test_distill_and_register() -> None:
    p = ServingPlatform()
    spec = DistillationSpec(
        "kinora-brain-mini", "1.0.0", "kinora-brain@1.0.0", compression=0.1, epochs=5
    )
    result = p.distill_and_register(spec, dataset_name="distill-default")
    assert p.registry.exists("kinora-brain-mini@1.0.0")
    assert result.student.teacher == "kinora-brain@1.0.0"
    # The student serves the same role and is cheaper than the teacher.
    teacher = p.registry.get("kinora-brain@1.0.0")
    assert result.student.kind == teacher.kind
    assert result.student.profile.cost_per_1k_tokens < teacher.profile.cost_per_1k_tokens


# -- gating + promotion ---------------------------------------------------- #


def test_gate_and_promote_lifecycle() -> None:
    p = ServingPlatform(gate=EvalGate(min_mean_reward=0.0, min_pass_rate=0.0))
    p.gate_model("kinora-reward@1.0.0", dataset_name="eval-default")
    final = p.promote("kinora-reward@1.0.0", Stage.PROD)
    assert final.stage == Stage.PROD
    prod = p.production("kinora-reward")
    assert prod is not None and prod.ref == "kinora-reward@1.0.0"


def test_promote_one_rung_without_target() -> None:
    p = ServingPlatform(gate=EvalGate(0.0, 0.0))
    p.gate_model("kinora-judge@1.0.0", dataset_name="eval-default")
    promoted = p.promote("kinora-judge@1.0.0")
    assert promoted.stage == Stage.STAGING


def test_rollback() -> None:
    p = ServingPlatform(gate=EvalGate(0.0, 0.0))
    p.gate_model("kinora-judge@1.0.0", dataset_name="eval-default")
    p.promote("kinora-judge@1.0.0", Stage.CANARY)
    rolled = p.rollback("kinora-judge@1.0.0")
    assert rolled.stage == Stage.STAGING


def test_gate_unknown_model_raises() -> None:
    p = ServingPlatform()
    with pytest.raises(ModelNotFoundError):
        p.gate_model("ghost@1.0.0", dataset_name="eval-default")


# -- simulation through the façade ----------------------------------------- #


def test_simulate_with_defaults() -> None:
    p = ServingPlatform()
    report = p.simulate("kinora-reward@1.0.0")
    assert report.n_completed == len(p.default_workload())
    assert report.tokens_per_s > 0


def test_simulate_smaller_model_is_faster() -> None:
    p = ServingPlatform()
    big = p.simulate("kinora-brain@1.0.0")
    small = p.simulate("kinora-reward@1.0.0")
    # The 7B reward model serves faster than the 72B brain on the same workload.
    assert small.wall_clock_ms < big.wall_clock_ms
    assert small.total_cost < big.total_cost


def test_plan_capacity_returns_ranked_plan() -> None:
    p = ServingPlatform()
    plan = p.plan_capacity("kinora-reward@1.0.0", objective="tokens_per_s")
    assert len(plan.candidates) > 0
    tps = [c.report.tokens_per_s for c in plan.candidates]
    assert tps == sorted(tps, reverse=True)


def test_recommend_config() -> None:
    p = ServingPlatform()
    rec = p.recommend_config("kinora-reward@1.0.0", p99_latency_slo_ms=1e9)
    assert rec.report.n_completed == len(p.default_workload())


def test_default_workload_is_deterministic() -> None:
    a = ServingPlatform.default_workload(seed="x", n=10)
    b = ServingPlatform.default_workload(seed="x", n=10)
    assert [r.request_id for r in a] == [r.request_id for r in b]
    assert len(a) == 10


def test_full_platform_lifecycle_distill_gate_promote_simulate() -> None:
    """End to end through the façade: distil → gate → promote → simulate the result,
    confirming the distilled student both passes gating and serves cheaper."""
    p = ServingPlatform(gate=EvalGate(0.0, 0.0))
    spec = DistillationSpec(
        "kinora-brain-mini", "1.0.0", "kinora-brain@1.0.0", compression=0.1, epochs=6
    )
    result = p.distill_and_register(spec, dataset_name="distill-default")
    gate = p.gate_model(result.student.ref, dataset_name="eval-default")
    assert gate.passed
    p.promote(result.student.ref, Stage.PROD)
    assert p.production("kinora-brain-mini") is not None

    mini = p.simulate(result.student.ref)
    brain = p.simulate("kinora-brain@1.0.0")
    assert mini.total_cost < brain.total_cost
    assert mini.tokens_per_s > brain.tokens_per_s
