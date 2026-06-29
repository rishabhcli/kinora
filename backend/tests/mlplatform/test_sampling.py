"""Sampling + class balancing: determinism, balance modes, weighted draws."""

from __future__ import annotations

import pytest

from app.mlplatform.datasets.contracts import AgentRole, Dataset, TaskType, TraceExample
from app.mlplatform.datasets.errors import DatasetError
from app.mlplatform.datasets.sampling import (
    BalanceMode,
    balance_by,
    reward_weight,
    role_key,
    stratified_subsample,
    subsample,
    weighted_sample,
)


def _ex(ex_id: str, *, role: AgentRole = AgentRole.ADAPTER, reward: float = 0.5) -> TraceExample:
    return TraceExample(
        id=ex_id,
        role=role,
        task=TaskType.SFT,
        prompt_key="adapter@v3",
        prompt_version="3.0.0",
        model="qwen-plus",
        input={"page_text": ex_id},
        output="o",
        reward=reward,
        book_id="bk0",
    )


def _skewed() -> Dataset:
    # 30 adapter, 6 critic — a majority/minority class skew.
    exs = [_ex(f"a{i}", role=AgentRole.ADAPTER) for i in range(30)]
    exs += [_ex(f"c{i}", role=AgentRole.CRITIC) for i in range(6)]
    return Dataset.from_examples("d", exs)


def test_subsample_size_and_determinism() -> None:
    ds = _skewed()
    a = subsample(ds, k=10, seed=7)
    b = subsample(ds, k=10, seed=7)
    assert len(a) == 10
    assert [e.id for e in a] == [e.id for e in b]
    # different seed → different selection
    c = subsample(ds, k=10, seed=99)
    assert [e.id for e in a] != [e.id for e in c]


def test_subsample_fraction() -> None:
    ds = _skewed()
    half = subsample(ds, fraction=0.5)
    assert len(half) == 18


def test_subsample_requires_k_or_fraction() -> None:
    with pytest.raises(DatasetError):
        subsample(_skewed())


def test_balance_undersample() -> None:
    ds, report = balance_by(_skewed(), role_key, mode=BalanceMode.UNDERSAMPLE)
    counts = report.after
    assert counts["adapter"] == counts["critic"] == 6  # capped at minority
    assert report.before["adapter"] == 30


def test_balance_oversample_makes_unique_ids() -> None:
    ds, report = balance_by(_skewed(), role_key, mode=BalanceMode.OVERSAMPLE)
    assert report.after["adapter"] == report.after["critic"] == 30
    # ids stay unique (oversampled copies get a #r suffix)
    ids = [e.id for e in ds.examples]
    assert len(ids) == len(set(ids))
    assert any("#r" in i for i in ids)


def test_balance_target() -> None:
    ds, report = balance_by(_skewed(), role_key, mode=BalanceMode.TARGET, target=12)
    assert report.after["adapter"] == 12
    assert report.after["critic"] == 12


def test_balance_target_requires_target() -> None:
    with pytest.raises(DatasetError):
        balance_by(_skewed(), role_key, mode=BalanceMode.TARGET)


def test_weighted_sample_favors_high_reward() -> None:
    exs = [_ex("hi", reward=0.99)] + [_ex(f"lo{i}", reward=0.01) for i in range(20)]
    ds = Dataset.from_examples("d", exs)
    sample = weighted_sample(ds, k=10, weight=reward_weight, seed=3)
    # the high-reward example should be drawn at least once
    assert any(e.id.startswith("hi") for e in sample)


def test_weighted_sample_without_replacement_size() -> None:
    ds = _skewed()
    s = weighted_sample(ds, k=5, with_replacement=False, seed=1)
    assert len(s) == 5
    ids = [e.id for e in s]
    assert len(ids) == len(set(ids))


def test_weighted_sample_empty_or_bad_k() -> None:
    with pytest.raises(DatasetError):
        weighted_sample(Dataset(name="e", examples=()), k=3)
    with pytest.raises(DatasetError):
        weighted_sample(_skewed(), k=0)


def test_stratified_subsample_preserves_shares() -> None:
    ds, _ = balance_by(_skewed(), role_key, mode=BalanceMode.TARGET, target=20)
    sub = stratified_subsample(ds, fraction=0.5, key=role_key)
    counts: dict[str, int] = {}
    for e in sub.examples:
        counts[e.role.value] = counts.get(e.role.value, 0) + 1
    # both classes halved (~10 each)
    assert counts["adapter"] == counts["critic"] == 10
