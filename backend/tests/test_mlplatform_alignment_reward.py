"""Correctness / convergence / calibration tests for the director reward model."""

from __future__ import annotations

import numpy as np
import pytest

from app.mlplatform.alignment.reward_model import (
    RewardModel,
    RewardModelTrainer,
)
from app.mlplatform.alignment.types import (
    PreferenceDataset,
    PreferencePair,
    Sample,
    SampleDataset,
)


def _make_separable_dataset(n: int = 200, seed: int = 0) -> SampleDataset:
    """Director accepts when ccs high AND artifacts low (a known boundary)."""

    rng = np.random.default_rng(seed)
    samples = []
    for _ in range(n):
        ccs = float(rng.uniform(0.6, 1.0))
        motion = float(rng.uniform(0.0, 1.0))  # already inverted: 1=clean
        # Accept iff ccs >= 0.85 and motion >= 0.75 (low artifacts).
        accept = ccs >= 0.85 and motion >= 0.75
        samples.append(Sample(features=[ccs, motion], reward=1.0 if accept else 0.0))
    return SampleDataset(samples=tuple(samples))


def test_reward_model_separates_accept_from_reject() -> None:
    ds = _make_separable_dataset()
    model = RewardModelTrainer(l2=0.01).fit(ds)
    assert model.converged
    metrics = model.evaluate(samples=ds)
    assert metrics.accuracy > 0.9
    assert metrics.auc > 0.95
    # A clearly-good clip scores high, a clearly-bad one scores low.
    good = model.reward([0.99, 0.95])
    bad = model.reward([0.6, 0.1])
    assert good > 0.7
    assert bad < 0.3
    assert good > bad


def test_reward_is_monotone_in_quality() -> None:
    ds = _make_separable_dataset(seed=3)
    model = RewardModelTrainer(l2=0.01).fit(ds)
    rs = [model.reward([c, 0.9]) for c in (0.6, 0.75, 0.85, 0.95, 1.0)]
    # Higher ccs (other axes fixed) never decreases the reward.
    assert all(b >= a - 1e-6 for a, b in zip(rs, rs[1:], strict=False))


def test_calibration_is_reasonable() -> None:
    # Probabilistic labels: accept-prob increases smoothly with feature.
    rng = np.random.default_rng(7)
    samples = []
    for _ in range(2000):
        x = float(rng.uniform(0.0, 1.0))
        p = 1.0 / (1.0 + np.exp(-(6.0 * (x - 0.5))))
        y = 1.0 if rng.uniform() < p else 0.0
        samples.append(Sample(features=[x], reward=y))
    ds = SampleDataset(samples=tuple(samples))
    model = RewardModelTrainer(l2=0.001).fit(ds)
    metrics = model.evaluate(samples=ds)
    # The logistic model matches the logistic DGP — ECE should be small.
    assert metrics.ece < 0.05


def test_pairwise_fit_ranks_correctly() -> None:
    # Director prefers the candidate with the higher first feature.
    rng = np.random.default_rng(11)
    pairs = []
    for _ in range(150):
        a = float(rng.uniform(0, 1))
        b = float(rng.uniform(0, 1))
        if abs(a - b) < 0.05:
            continue
        hi, lo = ([a, 0.5], [b, 0.5]) if a > b else ([b, 0.5], [a, 0.5])
        pairs.append(PreferencePair(winner=hi, loser=lo))
    pd = PreferenceDataset(pairs=tuple(pairs))
    model = RewardModelTrainer(l2=0.01).fit_pairwise(pd)
    metrics = model.evaluate(pairs=pd)
    assert metrics.pair_accuracy > 0.9
    # Logit increases with the first feature.
    assert model.logit([0.9, 0.5]) > model.logit([0.1, 0.5])


def test_combined_fit_is_calibrated_and_ranks() -> None:
    ds = _make_separable_dataset(n=150, seed=5)
    # Preference pairs consistent with the same boundary (prefer higher ccs).
    pairs = []
    for hi, lo in [
        ([0.95, 0.9], [0.7, 0.9]),
        ([0.9, 0.85], [0.65, 0.85]),
        ([0.99, 0.95], [0.8, 0.95]),
    ]:
        pairs.append(PreferencePair(winner=hi, loser=lo))
    pd = PreferenceDataset(pairs=tuple(pairs))
    model = RewardModelTrainer(l2=0.01, pairwise_weight=2.0).fit_combined(ds, pd)
    metrics = model.evaluate(samples=ds, pairs=pd)
    assert metrics.accuracy > 0.85
    assert metrics.pair_accuracy >= 0.9
    # Reward is still a probability in [0,1].
    r = model.reward([0.95, 0.9])
    assert 0.0 <= r <= 1.0


def test_rank_pair_symmetry() -> None:
    ds = _make_separable_dataset(seed=2)
    model = RewardModelTrainer().fit(ds)
    a, b = [0.95, 0.9], [0.7, 0.2]
    p_ab = model.rank_pair(a, b)
    p_ba = model.rank_pair(b, a)
    assert p_ab + p_ba == pytest.approx(1.0, abs=1e-9)
    assert p_ab > 0.5  # a is clearly the better clip


def test_serialization_roundtrip() -> None:
    ds = _make_separable_dataset(seed=4)
    model = RewardModelTrainer().fit(ds)
    d = model.to_dict()
    restored = RewardModel.from_dict(d)
    for feats in ([0.9, 0.9], [0.6, 0.1], [0.85, 0.75]):
        assert restored.reward(feats) == pytest.approx(model.reward(feats), abs=1e-12)


def test_dim_mismatch_raises() -> None:
    from app.mlplatform.alignment.errors import DataError

    model = RewardModelTrainer().fit(_make_separable_dataset(seed=1))
    with pytest.raises(DataError):
        model.reward([0.9])  # wrong dim


def test_edit_signal_weighted_into_fit() -> None:
    # A handful of edits with large magnitude push the boundary toward reject.
    base = [Sample([0.9, 0.9], 1.0) for _ in range(30)]
    base += [Sample([0.2, 0.2], 0.0) for _ in range(30)]
    edits = [
        Sample.from_signal([0.88, 0.5], "edit", edit_magnitude=0.9) for _ in range(10)
    ]
    ds = SampleDataset(samples=tuple(base + edits))
    model = RewardModelTrainer(l2=0.01).fit(ds)
    # The edited region (ccs 0.88, motion 0.5) should now score below 0.5.
    assert model.reward([0.88, 0.5]) < 0.5
