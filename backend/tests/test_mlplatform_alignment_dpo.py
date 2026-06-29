"""Correctness / convergence tests for the DPO preference-optimization module."""

from __future__ import annotations

import numpy as np
import pytest

from app.mlplatform.alignment.dpo import (
    DPOConfig,
    DPOPolicy,
    DPOTrainer,
    dpo_loss,
    preference_accuracy,
)
from app.mlplatform.alignment.errors import ConvergenceError, DataError
from app.mlplatform.alignment.linalg import Standardizer
from app.mlplatform.alignment.types import (
    PreferenceDataset,
    PreferencePair,
)


def _pref_dataset(seed: int = 0, n: int = 120) -> PreferenceDataset:
    """Director prefers the candidate with the higher first feature."""

    rng = np.random.default_rng(seed)
    pairs = []
    for _ in range(n):
        a = rng.uniform(0, 1, size=2)
        b = rng.uniform(0, 1, size=2)
        if abs(a[0] - b[0]) < 0.05:
            continue
        win, lose = (a, b) if a[0] > b[0] else (b, a)
        pairs.append(PreferencePair(winner=list(win), loser=list(lose)))
    return PreferenceDataset(pairs=tuple(pairs))


def test_dpo_config_validation() -> None:
    with pytest.raises(DataError):
        DPOConfig(beta=0.0)
    with pytest.raises(DataError):
        DPOConfig(lr=-1.0)
    with pytest.raises(DataError):
        DPOConfig(steps=0)
    with pytest.raises(DataError):
        DPOConfig(l2=-0.1)


def test_dpo_learns_to_rank() -> None:
    pd = _pref_dataset(seed=1)
    policy = DPOTrainer(DPOConfig(beta=0.2, lr=0.5, steps=800)).fit(pd)
    acc = preference_accuracy(policy, pd)
    assert acc > 0.9
    # The implicit reward increases along the preferred feature.
    assert policy.implicit_reward([0.9, 0.5]) > policy.implicit_reward([0.1, 0.5])


def test_dpo_loss_decreases_during_training() -> None:
    pd = _pref_dataset(seed=2)
    # Fit with few vs many steps; more training => lower loss.
    short = DPOTrainer(DPOConfig(beta=0.2, lr=0.3, steps=5)).fit(pd)
    long = DPOTrainer(DPOConfig(beta=0.2, lr=0.3, steps=800)).fit(pd)
    assert dpo_loss(long, pd) < dpo_loss(short, pd)


def test_dpo_is_deterministic() -> None:
    pd = _pref_dataset(seed=3)
    p1 = DPOTrainer(DPOConfig(beta=0.15, lr=0.4, steps=300)).fit(pd)
    p2 = DPOTrainer(DPOConfig(beta=0.15, lr=0.4, steps=300)).fit(pd)
    np.testing.assert_allclose(p1.theta, p2.theta, atol=1e-12)


def test_dpo_strict_raises_when_underbudget() -> None:
    pd = _pref_dataset(seed=4)
    with pytest.raises(ConvergenceError):
        DPOTrainer(DPOConfig(beta=0.2, lr=0.001, steps=2, tol=1e-12)).fit(
            pd, strict=True
        )


def test_vanishing_beta_pins_policy_to_reference() -> None:
    pd = _pref_dataset(seed=5)
    dim = pd.dim
    std = Standardizer.fit(
        np.vstack([[p.winner for p in pd], [p.loser for p in pd]])
    )
    ref = DPOPolicy(
        theta=np.zeros(dim), theta_ref=np.zeros(dim), beta=0.1, standardizer=std, dim=dim
    )
    # With a fixed L2 anchor, shrinking beta weakens the preference gradient
    # relative to the anchor, so the optimum sits closer to the reference.
    def _cfg(beta: float) -> DPOConfig:
        return DPOConfig(beta=beta, lr=0.2, steps=4000, l2=0.5)

    tiny = DPOTrainer(_cfg(0.02)).fit(pd, reference=ref)
    small = DPOTrainer(_cfg(0.1)).fit(pd, reference=ref)
    medium = DPOTrainer(_cfg(0.5)).fit(pd, reference=ref)
    assert tiny.converged and small.converged and medium.converged
    # Monotone: less KL temperature => the converged policy hugs the reference.
    assert tiny.deviation() < small.deviation() < medium.deviation()


def test_reference_from_reward_seeds_policy() -> None:
    # weights = [bias, w1, w2]; bias dropped, rest scaled by 1/beta.
    weights = np.array([0.5, 2.0, -1.0])
    std = Standardizer(mean=np.zeros(2), scale=np.ones(2))
    ref = DPOTrainer.reference_from_reward(weights, std, beta=0.5)
    assert ref.dim == 2
    np.testing.assert_allclose(ref.theta, ref.theta_ref)
    # At init the policy equals its reference => zero deviation.
    assert ref.deviation() == pytest.approx(0.0)


def test_dpo_serialization_roundtrip() -> None:
    pd = _pref_dataset(seed=6)
    policy = DPOTrainer(DPOConfig(beta=0.2, lr=0.4, steps=200)).fit(pd)
    restored = DPOPolicy.from_dict(policy.to_dict())
    for feats in ([0.9, 0.5], [0.1, 0.5], [0.5, 0.5]):
        assert restored.implicit_reward(feats) == pytest.approx(
            policy.implicit_reward(feats), abs=1e-12
        )


def test_dpo_empty_dataset_raises() -> None:
    # An empty PreferenceDataset is illegal at construction time.
    with pytest.raises(DataError):
        PreferenceDataset(pairs=())
