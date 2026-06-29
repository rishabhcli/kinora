"""Active preference learning — which pairs to ask the director to judge next.

Director attention is the scarcest resource in the §9.5 loop: every label costs a
human judgement. Active learning spends that budget where it buys the most signal
— on the candidate *pairs* the current reward model is most uncertain about (and
that are diverse enough not to all probe the same region twice).

Given a fitted :class:`RewardModel` and a pool of candidate clips (feature
vectors), this module ranks the ``C(n,2)`` possible comparison pairs by an
**acquisition score** and returns the top-k:

* **Uncertainty** — the model's ``P(A ≻ B)`` near 0.5 means the model can't tell
  the two apart; a director label there is maximally informative. Scored as
  ``1 - 2·|P - 0.5|`` (1 at a coin-flip, 0 at a confident ranking).
* **Magnitude** — both candidates should be plausibly good (high reward), so we
  don't waste a label distinguishing two clearly-bad clips.
* **Diversity** — greedy selection penalizes a new pair that is too close (in
  feature space) to an already-selected one, so the batch spans the pool.

Pure NumPy, deterministic (greedy with a fixed tie-break), fully unit-tested. No
model is *called* — the reward model is already fitted; we only read its scores.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .errors import DataError
from .linalg import Float, FloatArray
from .reward_model import RewardModel


@dataclass(frozen=True)
class PairQuery:
    """A proposed comparison the director should judge.

    ``i`` / ``j`` index into the candidate pool; ``prefer_prob`` is the model's
    current ``P(cand[i] ≻ cand[j])``; ``score`` is the acquisition value (higher =
    ask this first).
    """

    i: int
    j: int
    prefer_prob: float
    score: float


@dataclass(frozen=True)
class AcquisitionConfig:
    """Weights for the acquisition score.

    ``uncertainty_weight`` rewards near-coin-flip pairs; ``magnitude_weight``
    rewards pairs where both clips are decent; ``diversity_weight`` penalizes
    redundancy with already-selected pairs (0 disables diversity).
    """

    uncertainty_weight: float = 1.0
    magnitude_weight: float = 0.3
    diversity_weight: float = 0.5

    def __post_init__(self) -> None:
        for name, v in (
            ("uncertainty_weight", self.uncertainty_weight),
            ("magnitude_weight", self.magnitude_weight),
            ("diversity_weight", self.diversity_weight),
        ):
            if v < 0:
                raise DataError(f"{name} must be >= 0, got {v}")


def select_pairs(
    model: RewardModel,
    candidates: Sequence[Sequence[float]] | FloatArray,
    *,
    k: int = 5,
    config: AcquisitionConfig | None = None,
) -> list[PairQuery]:
    """Greedily select the ``k`` most informative comparison pairs to label.

    Builds the base acquisition score (uncertainty × magnitude) for every distinct
    pair, then greedily picks the highest, each time discounting remaining pairs by
    their feature-space similarity to the already-selected set (diversity). Returns
    up to ``k`` :class:`PairQuery`s, best first. Deterministic tie-break by
    ``(i, j)``.
    """

    cfg = config or AcquisitionConfig()
    cand = np.atleast_2d(np.asarray(candidates, dtype=Float))
    n = cand.shape[0]
    if n < 2:
        raise DataError("select_pairs needs at least 2 candidates")
    if k < 1:
        raise DataError("k must be >= 1")

    rewards = model.reward_batch(cand)  # 0..1 per candidate
    # Precompute pairwise base scores.
    pairs: list[tuple[int, int, float, float]] = []  # (i, j, prefer_prob, base)
    for i in range(n):
        for j in range(i + 1, n):
            p = model.rank_pair(cand[i], cand[j])
            uncertainty = 1.0 - 2.0 * abs(p - 0.5)  # 1 at coin-flip, 0 at certain
            magnitude = float(min(rewards[i], rewards[j]))  # both should be good
            base = (
                cfg.uncertainty_weight * uncertainty
                + cfg.magnitude_weight * magnitude
            )
            pairs.append((i, j, float(p), base))

    # Pairwise feature centroid for the diversity penalty.
    centroids = {
        (i, j): (cand[i] + cand[j]) / 2.0
        for (i, j, _p, _b) in pairs
    }
    feat_scale = float(np.linalg.norm(cand.std(axis=0)) + 1e-9)

    selected: list[PairQuery] = []
    remaining = list(pairs)
    chosen_centroids: list[FloatArray] = []
    while remaining and len(selected) < k:
        best_idx = -1
        best_val = -np.inf
        for idx, (i, j, _p, base) in enumerate(remaining):
            penalty = 0.0
            if cfg.diversity_weight > 0 and chosen_centroids:
                c = centroids[(i, j)]
                dists = [
                    float(np.linalg.norm(c - cc)) / feat_scale for cc in chosen_centroids
                ]
                # Closer to an existing pick (small distance) => larger penalty.
                nearest = min(dists)
                penalty = cfg.diversity_weight * np.exp(-nearest)
            val = base - penalty
            # Deterministic tie-break: prefer the lexicographically smaller pair.
            if val > best_val + 1e-12:
                best_val = val
                best_idx = idx
        i, j, p, base = remaining.pop(best_idx)
        selected.append(PairQuery(i=i, j=j, prefer_prob=p, score=float(best_val)))
        chosen_centroids.append(centroids[(i, j)])
    return selected


def labeling_priority(
    model: RewardModel, candidates: Sequence[Sequence[float]] | FloatArray
) -> FloatArray:
    """Per-candidate uncertainty: mean coin-flip-ness across all its pairings.

    A scalar 'how much would a label involving this candidate help' useful for
    prioritizing which *single* clips to surface for a thumbs-up/down, independent
    of pairing. Higher = more uncertain ⇒ more informative.
    """

    cand = np.atleast_2d(np.asarray(candidates, dtype=Float))
    n = cand.shape[0]
    if n < 2:
        raise DataError("labeling_priority needs at least 2 candidates")
    out = np.zeros(n, dtype=Float)
    for i in range(n):
        accum = 0.0
        for j in range(n):
            if i == j:
                continue
            p = model.rank_pair(cand[i], cand[j])
            accum += 1.0 - 2.0 * abs(p - 0.5)
        out[i] = accum / (n - 1)
    return out
