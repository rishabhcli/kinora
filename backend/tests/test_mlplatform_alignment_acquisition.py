"""Tests for active preference learning (acquisition / labeling priority)."""

from __future__ import annotations

import numpy as np
import pytest

from app.mlplatform.alignment.acquisition import (
    AcquisitionConfig,
    labeling_priority,
    select_pairs,
)
from app.mlplatform.alignment.errors import DataError
from app.mlplatform.alignment.reward_model import RewardModel, RewardModelTrainer
from app.mlplatform.alignment.types import Sample, SampleDataset


def _model(seed: int = 0) -> RewardModel:
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(300):
        x = float(rng.uniform(0, 1))
        rows.append(Sample([x, float(rng.uniform(0, 1))], 1.0 if x >= 0.5 else 0.0))
    return RewardModelTrainer(l2=0.05).fit(SampleDataset(samples=tuple(rows)))


def test_config_validation() -> None:
    with pytest.raises(DataError):
        AcquisitionConfig(uncertainty_weight=-1.0)


def test_select_pairs_returns_k_best() -> None:
    model = _model()
    cands = [[0.50, 0.5], [0.52, 0.5], [0.1, 0.5], [0.9, 0.5], [0.48, 0.5]]
    # No diversity penalty => the greedy scores are exactly the sorted base scores.
    cfg = AcquisitionConfig(diversity_weight=0.0)
    out = select_pairs(model, cands, k=3, config=cfg)
    assert len(out) == 3
    scores = [q.score for q in out]
    assert all(a >= b - 1e-9 for a, b in zip(scores, scores[1:], strict=False))
    # The selected pairs are distinct and within range.
    seen = {(q.i, q.j) for q in out}
    assert len(seen) == 3
    for q in out:
        assert 0 <= q.i < len(cands) and 0 <= q.j < len(cands) and q.i != q.j


def test_select_prefers_uncertain_pairs() -> None:
    model = _model(seed=1)
    # Pair (0.50 vs 0.52) is near the boundary => uncertain; (0.1 vs 0.9) is easy.
    cands = [[0.50, 0.5], [0.52, 0.5], [0.1, 0.5], [0.9, 0.5]]
    # Disable magnitude + diversity to isolate uncertainty.
    cfg = AcquisitionConfig(uncertainty_weight=1.0, magnitude_weight=0.0, diversity_weight=0.0)
    out = select_pairs(model, cands, k=1, config=cfg)
    top = out[0]
    # The top pair's prefer_prob is closer to 0.5 than the easy pair.
    easy_p = model.rank_pair(cands[2], cands[3])
    assert abs(top.prefer_prob - 0.5) < abs(easy_p - 0.5)


def test_diversity_spreads_the_batch() -> None:
    model = _model(seed=2)
    # Many near-duplicate uncertain pairs around 0.5, plus one elsewhere.
    cands = [[0.50, 0.5], [0.51, 0.5], [0.49, 0.5], [0.50, 0.9], [0.51, 0.9]]
    diverse = select_pairs(
        model, cands, k=2, config=AcquisitionConfig(diversity_weight=2.0)
    )
    # With strong diversity the two picks should not share an identical centroid.
    c0 = (np.array(cands[diverse[0].i]) + np.array(cands[diverse[0].j])) / 2
    c1 = (np.array(cands[diverse[1].i]) + np.array(cands[diverse[1].j])) / 2
    assert not np.allclose(c0, c1)


def test_select_pairs_validation() -> None:
    model = _model()
    with pytest.raises(DataError):
        select_pairs(model, [[0.5, 0.5]], k=1)  # < 2 candidates
    with pytest.raises(DataError):
        select_pairs(model, [[0.5, 0.5], [0.6, 0.5]], k=0)


def test_labeling_priority_high_for_boundary() -> None:
    model = _model(seed=3)
    cands = [[0.05, 0.5], [0.5, 0.5], [0.95, 0.5]]
    prio = labeling_priority(model, cands)
    assert prio.shape == (3,)
    # The boundary candidate (0.5) is the most uncertain on average.
    assert prio[1] >= prio[0] - 1e-9
    assert prio[1] >= prio[2] - 1e-9


def test_labeling_priority_validation() -> None:
    model = _model()
    with pytest.raises(DataError):
        labeling_priority(model, [[0.5, 0.5]])
