"""Unit tests for the model registry: versions, lineage, gates, promotion, rollback."""

from __future__ import annotations

import pytest

from app.mlplatform.serving.contracts import HeuristicRewardModel, synthetic_dataset
from app.mlplatform.serving.errors import (
    DuplicateModelVersionError,
    EvalGateError,
    LineageError,
    ModelNotFoundError,
    PromotionError,
    RollbackError,
)
from app.mlplatform.serving.model import ModelKind, ModelProfile, ModelVersion, Stage
from app.mlplatform.serving.registry import EvalGate, ModelRegistry


def _profile() -> ModelProfile:
    return ModelProfile(
        decode_ms_per_token=5.0,
        prefill_ms_per_token=0.5,
        kv_bytes_per_token=2048,
        params_billions=7.0,
        cost_per_1k_tokens=0.002,
    )


def _mv(name: str, version: str, **kw: object) -> ModelVersion:
    return ModelVersion(name, version, ModelKind.REASONING, _profile(), **kw)  # type: ignore[arg-type]


@pytest.fixture()
def reg() -> ModelRegistry:
    return ModelRegistry()


# -- registration / lookup ------------------------------------------------- #


def test_register_and_get(reg: ModelRegistry) -> None:
    mv = reg.register(_mv("brain", "1.0.0"))
    assert mv.stage == Stage.DEV
    assert reg.get("brain@1.0.0").ref == "brain@1.0.0"
    assert reg.exists("brain@1.0.0")


def test_register_duplicate_raises(reg: ModelRegistry) -> None:
    reg.register(_mv("brain", "1.0.0"))
    with pytest.raises(DuplicateModelVersionError):
        reg.register(_mv("brain", "1.0.0"))


def test_get_missing_raises_with_parsed_name(reg: ModelRegistry) -> None:
    with pytest.raises(ModelNotFoundError) as exc:
        reg.get("ghost@9.9.9")
    assert exc.value.name == "ghost"
    assert exc.value.version == "9.9.9"


def test_versions_of_sorted_and_latest(reg: ModelRegistry) -> None:
    reg.register(_mv("brain", "1.0.0"))
    reg.register(_mv("brain", "2.3.0"))
    reg.register(_mv("brain", "1.5.0"))
    vers = [v.version for v in reg.versions_of("brain")]
    assert vers == ["1.0.0", "1.5.0", "2.3.0"]
    assert reg.latest("brain").version == "2.3.0"


def test_versions_of_unknown_raises(reg: ModelRegistry) -> None:
    with pytest.raises(ModelNotFoundError):
        reg.versions_of("nope")


# -- lineage --------------------------------------------------------------- #


def test_lineage_parent_chain(reg: ModelRegistry) -> None:
    reg.register(_mv("brain", "1.0.0"))
    reg.register(_mv("brain", "2.0.0", parent="brain@1.0.0"))
    reg.register(_mv("brain", "3.0.0", parent="brain@2.0.0"))
    chain = [v.ref for v in reg.lineage("brain@3.0.0")]
    assert chain == ["brain@1.0.0", "brain@2.0.0", "brain@3.0.0"]


def test_register_unknown_parent_raises(reg: ModelRegistry) -> None:
    with pytest.raises(LineageError, match="parent"):
        reg.register(_mv("brain", "2.0.0", parent="brain@1.0.0"))


def test_register_unknown_teacher_raises(reg: ModelRegistry) -> None:
    with pytest.raises(LineageError, match="teacher"):
        reg.register(_mv("student", "1.0.0", teacher="teacher@1.0.0"))


def test_students_of(reg: ModelRegistry) -> None:
    reg.register(_mv("teacher", "1.0.0"))
    reg.register(_mv("student-a", "1.0.0", teacher="teacher@1.0.0"))
    reg.register(_mv("student-b", "1.0.0", teacher="teacher@1.0.0"))
    students = {v.name for v in reg.students_of("teacher@1.0.0")}
    assert students == {"student-a", "student-b"}


def test_students_of_unknown_teacher_raises(reg: ModelRegistry) -> None:
    with pytest.raises(ModelNotFoundError):
        reg.students_of("ghost@1.0.0")


# -- eval gate ------------------------------------------------------------- #


def test_eval_gate_validation() -> None:
    with pytest.raises(EvalGateError):
        EvalGate(min_mean_reward=2.0)
    with pytest.raises(EvalGateError):
        EvalGate(min_pass_rate=-0.1)


def test_eval_gate_empty_dataset_raises() -> None:
    gate = EvalGate()
    ds = synthetic_dataset("empty", size=0)
    with pytest.raises(EvalGateError, match="empty"):
        gate.run("m@1.0.0", ds, HeuristicRewardModel())


def test_run_gate_records_pass_and_permits_promotion(reg: ModelRegistry) -> None:
    reg.register(_mv("brain", "1.0.0"))
    ds = synthetic_dataset("eval", size=30)
    # A floor of 0 always passes; verifies the wiring + that the row records it.
    gate = EvalGate(min_mean_reward=0.0, min_pass_rate=0.0)
    result = reg.run_gate("brain@1.0.0", gate, ds, HeuristicRewardModel())
    assert result.passed is True
    assert result.n_cases == 30
    assert reg.get("brain@1.0.0").gate_passed is True
    assert "gate" in {e.action for e in reg.history("brain@1.0.0")}


def test_run_gate_failure_blocks_promotion(reg: ModelRegistry) -> None:
    reg.register(_mv("brain", "1.0.0"))
    ds = synthetic_dataset("eval", size=20)
    # An impossible floor fails the gate.
    gate = EvalGate(min_mean_reward=1.0, min_pass_rate=1.0)
    result = reg.run_gate("brain@1.0.0", gate, ds, HeuristicRewardModel())
    assert result.passed is False
    assert result.reasons
    assert reg.get("brain@1.0.0").gate_passed is False
    with pytest.raises(PromotionError, match="eval gate has not passed"):
        reg.promote("brain@1.0.0")


def test_gate_result_as_dict_roundtrips(reg: ModelRegistry) -> None:
    reg.register(_mv("brain", "1.0.0"))
    ds = synthetic_dataset("eval", size=10)
    result = reg.run_gate("brain@1.0.0", EvalGate(0.0, 0.0), ds, HeuristicRewardModel())
    d = result.as_dict()
    assert d["passed"] is True
    assert d["n_cases"] == 10


# -- promotion / rollback -------------------------------------------------- #


def _gate_pass(reg: ModelRegistry, ref: str) -> None:
    ds = synthetic_dataset("g", size=5)
    reg.run_gate(ref, EvalGate(0.0, 0.0), ds, HeuristicRewardModel())


def test_promote_one_rung_at_a_time(reg: ModelRegistry) -> None:
    reg.register(_mv("brain", "1.0.0"))
    _gate_pass(reg, "brain@1.0.0")
    assert reg.promote("brain@1.0.0").stage == Stage.STAGING
    assert reg.promote("brain@1.0.0").stage == Stage.CANARY
    assert reg.promote("brain@1.0.0").stage == Stage.PROD
    with pytest.raises(PromotionError, match="top stage"):
        reg.promote("brain@1.0.0")


def test_promote_to_loops_each_rung(reg: ModelRegistry) -> None:
    reg.register(_mv("brain", "1.0.0"))
    _gate_pass(reg, "brain@1.0.0")
    final = reg.promote_to("brain@1.0.0", Stage.PROD)
    assert final.stage == Stage.PROD
    promotes = [e for e in reg.history("brain@1.0.0") if e.action == "promote"]
    assert len(promotes) == 3  # dev->staging->canary->prod


def test_promote_to_rejects_non_promotable_target(reg: ModelRegistry) -> None:
    reg.register(_mv("brain", "1.0.0"))
    with pytest.raises(PromotionError):
        reg.promote_to("brain@1.0.0", Stage.DEV)


def test_promote_demotes_incumbent_in_target_rung(reg: ModelRegistry) -> None:
    reg.register(_mv("brain", "1.0.0"))
    reg.register(_mv("brain", "2.0.0"))
    _gate_pass(reg, "brain@1.0.0")
    _gate_pass(reg, "brain@2.0.0")
    reg.promote_to("brain@1.0.0", Stage.PROD)
    prod_v1 = reg.production("brain")
    assert prod_v1 is not None and prod_v1.version == "1.0.0"
    # Promote v2 up to prod; v1 (the incumbent) is demoted to canary.
    reg.promote_to("brain@2.0.0", Stage.PROD)
    prod_v2 = reg.production("brain")
    assert prod_v2 is not None and prod_v2.version == "2.0.0"
    assert reg.get("brain@1.0.0").stage == Stage.CANARY


def test_rollback_moves_down_one_rung(reg: ModelRegistry) -> None:
    reg.register(_mv("brain", "1.0.0"))
    _gate_pass(reg, "brain@1.0.0")
    reg.promote_to("brain@1.0.0", Stage.CANARY)
    assert reg.rollback("brain@1.0.0").stage == Stage.STAGING
    assert reg.rollback("brain@1.0.0").stage == Stage.DEV
    with pytest.raises(RollbackError, match="bottom stage"):
        reg.rollback("brain@1.0.0")


def test_promote_archived_raises(reg: ModelRegistry) -> None:
    reg.register(_mv("brain", "1.0.0"))
    reg.archive("brain@1.0.0")
    assert reg.get("brain@1.0.0").stage == Stage.ARCHIVED
    with pytest.raises(PromotionError, match="archived"):
        reg.promote("brain@1.0.0")


def test_serving_set_returns_prod_only(reg: ModelRegistry) -> None:
    reg.register(_mv("brain", "1.0.0"))
    reg.register(ModelVersion("judge", "1.0.0", ModelKind.JUDGE, _profile()))
    _gate_pass(reg, "brain@1.0.0")
    reg.promote_to("brain@1.0.0", Stage.PROD)
    serving = reg.serving_set()
    assert [v.ref for v in serving] == ["brain@1.0.0"]
    assert reg.serving_set(kind=ModelKind.JUDGE) == ()


def test_history_filtered_by_ref(reg: ModelRegistry) -> None:
    reg.register(_mv("a", "1.0.0"))
    reg.register(_mv("b", "1.0.0"))
    assert all(e.model_ref == "a@1.0.0" for e in reg.history("a@1.0.0"))
    assert len(reg.history()) >= 2
