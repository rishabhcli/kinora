"""Unit tests for rubric scoring + datasets (no infra)."""

from __future__ import annotations

import pytest

from app.llmops.datasets import from_dicts, get_dataset
from app.llmops.errors import DatasetError, RubricError
from app.llmops.rubric import RUBRICS, Criterion, Rubric, get_rubric, score


def test_weighted_mean() -> None:
    rubric = Rubric(
        name="t",
        threshold=0.5,
        criteria=(
            Criterion("a", weight=3.0),
            Criterion("b", weight=1.0),
        ),
    )
    result = score(rubric, {"a": 1.0, "b": 0.0})
    # (1*3 + 0*1) / 4 = 0.75
    assert result.overall == 0.75
    assert result.passed


def test_missing_criterion_scores_zero() -> None:
    rubric = Rubric(name="t", criteria=(Criterion("a"), Criterion("b")))
    result = score(rubric, {"a": 1.0})  # b missing
    assert result.per_criterion["b"] == 0.0


def test_clamping() -> None:
    rubric = Rubric(name="t", criteria=(Criterion("a"),))
    assert score(rubric, {"a": 5.0}).overall == 1.0
    assert score(rubric, {"a": -3.0}).overall == 0.0


def test_required_criterion_hard_gate() -> None:
    rubric = Rubric(
        name="t",
        threshold=0.5,
        criteria=(
            Criterion("valid", required=True, min_score=1.0, weight=1.0),
            Criterion("quality", weight=4.0),
        ),
    )
    # Overall is high (0.8) but the required gate fails -> not passed.
    result = score(rubric, {"valid": 0.0, "quality": 1.0})
    assert result.overall == 0.8
    assert not result.passed
    assert "valid" in result.failed_required


def test_rubric_validation() -> None:
    with pytest.raises(RubricError):
        Rubric(name="empty", criteria=())
    with pytest.raises(RubricError):
        Rubric(name="dup", criteria=(Criterion("a"), Criterion("a")))
    with pytest.raises(RubricError):
        Rubric(name="bad-threshold", criteria=(Criterion("a"),), threshold=2.0)
    with pytest.raises(RubricError):
        Criterion("neg", weight=-1.0)


def test_builtin_rubrics_present() -> None:
    for name in (
        "json_contract",
        "adapter_quality",
        "cinematographer_quality",
        "critic_quality",
        "safety",
    ):
        assert name in RUBRICS
        assert get_rubric(name).name == name
    with pytest.raises(RubricError):
        get_rubric("does_not_exist")


def test_get_dataset_and_from_dicts() -> None:
    ds = get_dataset("adapter_golden_v1")
    assert len(ds) >= 1
    assert ds.rubric_name == "adapter_quality"
    with pytest.raises(DatasetError):
        get_dataset("nope")

    built = from_dicts(
        "custom",
        "json_contract",
        [{"id": "c1", "inputs": {"x": 1}, "expected_keys": ["y"], "must_include": "foo"}],
    )
    assert built.cases[0].expected_keys == ("y",)
    assert built.cases[0].must_include == ("foo",)


def test_dataset_rejects_unknown_rubric() -> None:
    with pytest.raises(DatasetError):
        from_dicts("bad", "no_such_rubric", [{"id": "c1", "inputs": {}}])


def test_injection_dataset_is_adversarial() -> None:
    ds = get_dataset("injection_probes_v1")
    assert ds.adversarial_count == len(ds)
    assert ds.rubric_name == "safety"
