"""Unit tests for the cross-facet contracts + offline fakes (no infra)."""

from __future__ import annotations

import pytest

from app.mlplatform.serving.contracts import (
    Dataset,
    DatasetCase,
    DatasetSource,
    HeuristicRewardModel,
    RewardModel,
    RewardScore,
    StaticDatasetSource,
    _reference_overlap,
    _seeded_unit,
    synthetic_dataset,
)


def test_dataset_case_defaults() -> None:
    c = DatasetCase(case_id="c1", inputs={"prompt": "hi"})
    assert c.reference is None
    assert c.tags == ()


def test_dataset_rejects_duplicate_case_ids() -> None:
    cases = (DatasetCase("a", {}), DatasetCase("a", {}))
    with pytest.raises(ValueError, match="duplicate case ids"):
        Dataset(name="d", version="1.0.0", cases=cases)


def test_dataset_len_iter_and_filter_tag() -> None:
    ds = synthetic_dataset("eval", size=9)
    assert len(ds) == 9
    assert [c.case_id for c in ds][:1] == ["eval-0000"]
    hard = ds.filter_tag("hard")
    assert all("hard" in c.tags for c in hard)
    assert 0 < len(hard) < len(ds)
    assert hard.name == "eval:hard"


def test_synthetic_dataset_is_deterministic() -> None:
    a = synthetic_dataset("x", size=20)
    b = synthetic_dataset("x", size=20)
    assert [c.reference for c in a] == [c.reference for c in b]


def test_synthetic_dataset_rejects_negative() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        synthetic_dataset("x", size=-1)


def test_seeded_unit_is_in_range_and_stable() -> None:
    for parts in (("a",), ("a", "b"), ("x", "y", "z")):
        u = _seeded_unit(*parts)
        assert 0.0 <= u < 1.0
        assert _seeded_unit(*parts) == u  # stable


def test_seeded_unit_distinguishes_inputs() -> None:
    assert _seeded_unit("a", "b") != _seeded_unit("b", "a")


def test_reward_score_validates_range() -> None:
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        RewardScore(value=1.5, passed=True)
    s = RewardScore(value=0.5, passed=False)
    assert s.axes == {}


def test_reference_overlap_bounds() -> None:
    assert _reference_overlap(None, "anything") == 0.5
    assert _reference_overlap("a b c", "a b c") == 1.0
    assert _reference_overlap("a b", "c d") == 0.0
    val = _reference_overlap("a b c d", "a b x y")
    assert 0.0 < val < 1.0


def test_static_dataset_source_satisfies_protocol() -> None:
    ds = synthetic_dataset("d1", size=3)
    src = StaticDatasetSource([ds])
    assert isinstance(src, DatasetSource)
    assert src.names() == ("d1",)
    assert src.get("d1") is ds
    with pytest.raises(KeyError):
        src.get("missing")


def test_static_dataset_source_rejects_duplicate_names() -> None:
    ds = synthetic_dataset("dup", size=1)
    with pytest.raises(ValueError, match="duplicate dataset name"):
        StaticDatasetSource([ds, ds])


def test_heuristic_reward_model_satisfies_protocol_and_is_deterministic() -> None:
    rm = HeuristicRewardModel()
    assert isinstance(rm, RewardModel)
    case = DatasetCase("c", {"prompt": "p"}, reference="a wide cinematic shot")
    s1 = rm.score(case, "a wide cinematic shot")
    s2 = rm.score(case, "a wide cinematic shot")
    assert s1 == s2
    assert 0.0 <= s1.value <= 1.0
    assert set(s1.axes) == {"fluency", "fidelity"}


def test_heuristic_reward_higher_for_better_overlap() -> None:
    rm = HeuristicRewardModel()
    case = DatasetCase("c", {}, reference="the quick brown fox jumps")
    good = rm.score(case, "the quick brown fox jumps").value
    bad = rm.score(case, "completely unrelated tokens here").value
    assert good > bad


def test_heuristic_reward_base_quality_shifts_distribution() -> None:
    case = DatasetCase("c", {}, reference="ref text")
    low = HeuristicRewardModel(base_quality=-1.0).score(case, "x").value
    high = HeuristicRewardModel(base_quality=1.0).score(case, "x").value
    assert high >= low


def test_heuristic_reward_base_quality_validated() -> None:
    with pytest.raises(ValueError, match=r"\[-1, 1\]"):
        HeuristicRewardModel(base_quality=2.0)
