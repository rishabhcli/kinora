"""Unit tests for the knowledge-distillation pipeline (no infra)."""

from __future__ import annotations

import pytest

from app.mlplatform.serving.contracts import (
    HeuristicRewardModel,
    synthetic_dataset,
)
from app.mlplatform.serving.distillation import (
    DistillationPipeline,
    DistillationSpec,
    _learning_curve,
    _student_profile,
)
from app.mlplatform.serving.errors import (
    DistillationSpecError,
    EmptyCorpusError,
    LineageError,
)
from app.mlplatform.serving.model import ModelKind, ModelProfile, ModelVersion, Stage
from app.mlplatform.serving.registry import EvalGate, ModelRegistry


def _teacher_profile() -> ModelProfile:
    return ModelProfile(
        decode_ms_per_token=20.0,
        prefill_ms_per_token=2.0,
        kv_bytes_per_token=8192,
        params_billions=70.0,
        cost_per_1k_tokens=0.02,
    )


@pytest.fixture()
def reg_with_teacher() -> ModelRegistry:
    reg = ModelRegistry()
    reg.register(ModelVersion("brain", "1.0.0", ModelKind.REASONING, _teacher_profile()))
    return reg


# -- spec validation ------------------------------------------------------- #


def test_spec_validation() -> None:
    base: dict[str, object] = {
        "student_name": "s",
        "student_version": "1.0.0",
        "teacher_ref": "t@1.0.0",
    }
    bad_cases: tuple[dict[str, object], ...] = (
        {"student_name": ""},
        {"compression": 0.0},
        {"compression": 1.0},
        {"quality_retention": 0.0},
        {"quality_retention": 1.5},
        {"epochs": 0},
        {"reward_floor": 1.5},
    )
    for bad in bad_cases:
        kw = dict(base)
        kw.update(bad)
        with pytest.raises(DistillationSpecError):
            DistillationSpec(**kw)  # type: ignore[arg-type]


# -- learning curve + profile derivation ----------------------------------- #


def test_learning_curve_is_monotonic_and_bounded() -> None:
    prev = 0.0
    for epochs in range(1, 12):
        v = _learning_curve(0.9, epochs)
        assert v >= prev
        assert 0.0 <= v <= 0.9
        prev = v


def test_student_profile_is_cheaper_than_teacher() -> None:
    teacher = _teacher_profile()
    student = _student_profile(teacher, compression=0.1)
    assert student.decode_ms_per_token < teacher.decode_ms_per_token
    assert student.prefill_ms_per_token < teacher.prefill_ms_per_token
    assert student.kv_bytes_per_token < teacher.kv_bytes_per_token
    assert student.params_billions < teacher.params_billions
    assert student.cost_per_1k_tokens < teacher.cost_per_1k_tokens
    # Memory scales linearly with compression; compute sub-linearly (sqrt).
    assert student.params_billions == pytest.approx(7.0)
    assert student.kv_bytes_per_token == pytest.approx(819, abs=1)


# -- corpus generation ----------------------------------------------------- #


def test_generate_corpus_labels_every_case(reg_with_teacher: ModelRegistry) -> None:
    pipe = DistillationPipeline(reg_with_teacher)
    spec = DistillationSpec("s", "1.0.0", "brain@1.0.0")
    ds = synthetic_dataset("corpus", size=12)
    corpus = pipe.generate_corpus(spec, ds, reward=HeuristicRewardModel())
    assert len(corpus) == 12
    assert all(e.teacher_output for e in corpus)
    assert all(0.0 <= e.teacher_reward <= 1.0 for e in corpus)


def test_generate_corpus_without_reward_trusts_teacher(reg_with_teacher: ModelRegistry) -> None:
    pipe = DistillationPipeline(reg_with_teacher)
    spec = DistillationSpec("s", "1.0.0", "brain@1.0.0", reward_floor=0.9)
    ds = synthetic_dataset("corpus", size=5)
    corpus = pipe.generate_corpus(spec, ds, reward=None)
    assert all(e.teacher_reward == 1.0 for e in corpus)
    assert all(e.kept for e in corpus)


def test_generate_corpus_unknown_teacher_raises(reg_with_teacher: ModelRegistry) -> None:
    pipe = DistillationPipeline(reg_with_teacher)
    spec = DistillationSpec("s", "1.0.0", "ghost@1.0.0")
    with pytest.raises(LineageError):
        pipe.generate_corpus(spec, synthetic_dataset("c", size=3))


def test_generate_corpus_empty_dataset_raises(reg_with_teacher: ModelRegistry) -> None:
    pipe = DistillationPipeline(reg_with_teacher)
    spec = DistillationSpec("s", "1.0.0", "brain@1.0.0")
    with pytest.raises(EmptyCorpusError):
        pipe.generate_corpus(spec, synthetic_dataset("c", size=0))


def test_reward_floor_filters_examples(reg_with_teacher: ModelRegistry) -> None:
    pipe = DistillationPipeline(reg_with_teacher)
    ds = synthetic_dataset("corpus", size=40)
    # A high floor drops some examples; corpus keeps all, kept marks a subset.
    spec = DistillationSpec("s", "1.0.0", "brain@1.0.0", reward_floor=0.55)
    corpus = pipe.generate_corpus(spec, ds, reward=HeuristicRewardModel())
    kept = [e for e in corpus if e.kept]
    assert 0 < len(kept) < len(corpus)


# -- full distill ---------------------------------------------------------- #


def test_distill_registers_student_with_lineage(reg_with_teacher: ModelRegistry) -> None:
    pipe = DistillationPipeline(reg_with_teacher)
    ds = synthetic_dataset("corpus", size=30)
    spec = DistillationSpec(
        "brain-mini", "1.0.0", "brain@1.0.0", compression=0.1, epochs=4, reward_floor=0.2
    )
    result = pipe.distill(spec, ds, reward=HeuristicRewardModel())
    assert result.student.ref == "brain-mini@1.0.0"
    assert result.student.teacher == "brain@1.0.0"
    assert result.student.stage == Stage.DEV
    assert "distilled" in result.student.tags
    # Registered → resolvable + appears as a student of the teacher.
    assert reg_with_teacher.exists("brain-mini@1.0.0")
    assert reg_with_teacher.students_of("brain@1.0.0")[0].name == "brain-mini"
    assert 0.0 <= result.modeled_student_quality <= 1.0


def test_distill_no_register(reg_with_teacher: ModelRegistry) -> None:
    pipe = DistillationPipeline(reg_with_teacher)
    ds = synthetic_dataset("corpus", size=10)
    spec = DistillationSpec("ephemeral", "1.0.0", "brain@1.0.0")
    result = pipe.distill(spec, ds, reward=None, register=False)
    assert not reg_with_teacher.exists("ephemeral@1.0.0")
    assert result.student.name == "ephemeral"


def test_distill_all_filtered_raises(reg_with_teacher: ModelRegistry) -> None:
    pipe = DistillationPipeline(reg_with_teacher)
    ds = synthetic_dataset("corpus", size=10)
    # An impossible floor drops everything → no training data.
    spec = DistillationSpec("s", "1.0.0", "brain@1.0.0", reward_floor=1.0)
    with pytest.raises(EmptyCorpusError, match="reward_floor"):
        pipe.distill(spec, ds, reward=HeuristicRewardModel())


def test_distill_is_deterministic(reg_with_teacher: ModelRegistry) -> None:
    ds = synthetic_dataset("corpus", size=20)
    spec1 = DistillationSpec("a", "1.0.0", "brain@1.0.0")
    spec2 = DistillationSpec("a", "1.0.0", "brain@1.0.0")
    r1 = DistillationPipeline(reg_with_teacher).distill(spec1, ds, register=False)
    r2 = DistillationPipeline(reg_with_teacher).distill(spec2, ds, register=False)
    assert r1.modeled_student_quality == r2.modeled_student_quality


def test_more_epochs_yields_higher_quality(reg_with_teacher: ModelRegistry) -> None:
    ds = synthetic_dataset("corpus", size=30)
    pipe = DistillationPipeline(reg_with_teacher)
    low = pipe.distill(
        DistillationSpec("low", "1.0.0", "brain@1.0.0", epochs=1), ds, register=False
    )
    high = pipe.distill(
        DistillationSpec("high", "1.0.0", "brain@1.0.0", epochs=8), ds, register=False
    )
    assert high.modeled_student_quality > low.modeled_student_quality


def test_full_lifecycle_distill_gate_promote(reg_with_teacher: ModelRegistry) -> None:
    """The end-to-end model lifecycle: distil a student, gate it on an eval set,
    then promote it to production."""
    reg = reg_with_teacher
    pipe = DistillationPipeline(reg)
    corpus = synthetic_dataset("train", size=40)
    spec = DistillationSpec("brain-mini", "1.0.0", "brain@1.0.0", compression=0.1, epochs=6)
    result = pipe.distill(spec, corpus, reward=HeuristicRewardModel())
    student_ref = result.student.ref

    # Gate the student on a held-out eval set (floors at 0 to guarantee a pass here).
    eval_ds = synthetic_dataset("eval", size=20)
    gate = EvalGate(min_mean_reward=0.0, min_pass_rate=0.0)
    gate_result = reg.run_gate(student_ref, gate, eval_ds, HeuristicRewardModel())
    assert gate_result.passed

    # Promote through the ladder to production.
    final = reg.promote_to(student_ref, Stage.PROD)
    assert final.stage == Stage.PROD
    prod = reg.production("brain-mini")
    assert prod is not None and prod.ref == student_ref
    # The student is now part of the served set and is cheaper than its teacher.
    serving = reg.serving_set(kind=ModelKind.REASONING)
    served = {v.ref for v in serving}
    assert student_ref in served
    teacher = reg.get("brain@1.0.0")
    assert final.profile.decode_ms_per_token < teacher.profile.decode_ms_per_token
