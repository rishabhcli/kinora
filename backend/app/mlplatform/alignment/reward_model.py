"""The director reward model — RLHF's learned scalar reward (§9.5, §13).

This is the alignment platform's foundation: a calibrated model ``r(x) ∈ [0, 1]``
that predicts *"the director would accept this candidate clip"* from an
already-measured feature vector (the QA axes + aesthetic / temporal extras). It
is trained on the two director signals the system accumulates:

* **pointwise** accept / reject / edit / degrade labels (:class:`Sample`), via
  L2-regularized logistic regression (Bradley–Terry's pointwise special case);
* **pairwise** "A ≻ B" judgements (:class:`PreferencePair`), via a Bradley–Terry
  logistic over the feature *difference* — the canonical preference model RLHF
  reward models use.

Both can be combined into one weight vector (``fit_combined``): the pointwise
likelihood anchors the *absolute* scale (so a reward is interpretable as an
accept-probability) while the pairwise likelihood sharpens the *ranking*.

Everything is deterministic (fixed init, no sampling) and pure NumPy, so the
math is exhaustively unit-tested for correctness and convergence with no network,
no DB, and zero video-seconds. The model is **distinct** from the Critic's
``app/render/reward.py`` sketch: this is a trainable, serializable platform model
with combined pointwise+pairwise objectives, calibration, and ranking metrics —
the substrate the DPO / policy-eval / A/B layers build on.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import DataError
from .linalg import (
    Features,
    Float,
    FloatArray,
    Standardizer,
    add_bias,
    expected_calibration_error,
    fit_logistic,
    sigmoid,
)
from .types import (
    PreferenceDataset,
    PreferencePair,
    Sample,
    SampleDataset,
    as_sample_dataset,
)


@dataclass(frozen=True)
class RewardMetrics:
    """Held-out quality of a fitted reward model.

    ``accuracy`` / ``auc`` measure the pointwise accept-classification; ``ece`` is
    calibration (lower is better); ``pair_accuracy`` is the fraction of preference
    pairs the model ranks correctly; ``log_loss`` is the pointwise NLL.
    """

    accuracy: float
    auc: float
    ece: float
    log_loss: float
    pair_accuracy: float
    n_samples: int
    n_pairs: int


@dataclass(frozen=True)
class RewardModel:
    """A fitted, serializable director reward model.

    ``weights[0]`` is the bias; the remainder are per-feature coefficients in the
    *standardized* space described by ``standardizer``. :meth:`reward` maps a raw
    feature vector to ``[0, 1]``. The model is frozen and round-trips through
    :meth:`to_dict` / :meth:`from_dict` for experiment tracking + the FT
    orchestrator's artifact store.
    """

    weights: FloatArray
    standardizer: Standardizer
    dim: int
    converged: bool = True
    feature_names: tuple[str, ...] = ()
    train_objective: str = "pointwise"

    def _design(self, features: Features) -> FloatArray:
        x = np.atleast_2d(np.asarray(features, dtype=Float))
        if x.shape[1] != self.dim:
            raise DataError(f"expected {self.dim} features, got {x.shape[1]}")
        return add_bias(self.standardizer.transform(x))

    def reward(self, features: Features) -> float:
        """Scalar reward ``P(accept)`` for a single feature vector."""

        return float(self.reward_batch(features)[0])

    def reward_batch(self, features: Features) -> FloatArray:
        """Vectorized :meth:`reward` over a stack of feature vectors."""

        z = self._design(features) @ self.weights
        return sigmoid(z)

    def logit(self, features: Features) -> float:
        """The pre-sigmoid score — an *unbounded* reward for ranking / DPO."""

        return float((self._design(features) @ self.weights).ravel()[0])

    def rank_pair(self, winner: Features, loser: Features) -> float:
        """``P(winner ≻ loser)`` under the Bradley–Terry head (logit difference)."""

        zw = self._design(winner) @ self.weights
        zl = self._design(loser) @ self.weights
        return float(sigmoid(zw - zl)[0])

    def evaluate(
        self,
        samples: SampleDataset | None = None,
        pairs: PreferenceDataset | None = None,
    ) -> RewardMetrics:
        """Compute held-out :class:`RewardMetrics` on samples and/or pairs."""

        acc = auc = ece = log_loss = 0.0
        n_samples = 0
        if samples is not None and len(samples) > 0:
            feats = np.array([s.features for s in samples], dtype=Float)
            y = np.array(samples.rewards, dtype=Float)
            yb = (y >= 0.5).astype(Float)
            p = self.reward_batch(feats)
            acc = float(np.mean((p >= 0.5).astype(Float) == yb))
            auc = _roc_auc(yb, p)
            ece = expected_calibration_error(p, yb)
            pc = np.clip(p, 1e-12, 1 - 1e-12)
            log_loss = float(-np.mean(yb * np.log(pc) + (1 - yb) * np.log(1 - pc)))
            n_samples = len(samples)
        pair_acc = 0.0
        n_pairs = 0
        if pairs is not None and len(pairs) > 0:
            wins = 0
            for pr in pairs:
                if self.rank_pair(np.array(pr.winner), np.array(pr.loser)) > 0.5:
                    wins += 1
            n_pairs = len(pairs)
            pair_acc = wins / n_pairs
        return RewardMetrics(
            accuracy=acc,
            auc=auc,
            ece=ece,
            log_loss=log_loss,
            pair_accuracy=pair_acc,
            n_samples=n_samples,
            n_pairs=n_pairs,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "weights": [float(w) for w in self.weights],
            "mean": [float(m) for m in self.standardizer.mean],
            "scale": [float(s) for s in self.standardizer.scale],
            "dim": self.dim,
            "converged": self.converged,
            "feature_names": list(self.feature_names),
            "train_objective": self.train_objective,
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> RewardModel:
        return cls(
            weights=np.array(d["weights"], dtype=Float),
            standardizer=Standardizer(
                mean=np.array(d["mean"], dtype=Float),
                scale=np.array(d["scale"], dtype=Float),
            ),
            dim=int(d["dim"]),  # type: ignore[call-overload]
            converged=bool(d.get("converged", True)),
            feature_names=tuple(d.get("feature_names", []) or []),  # type: ignore[arg-type]
            train_objective=str(d.get("train_objective", "pointwise")),
        )


@dataclass
class RewardModelTrainer:
    """Fits :class:`RewardModel`s from director signals.

    Stateless aside from hyper-parameters, so one trainer can fit many models. The
    ``l2`` ridge keeps the fit finite even on separable / tiny data (the cold-start
    regime), and ``pairwise_weight`` trades the pointwise (absolute-scale) and
    pairwise (ranking) objectives in :meth:`fit_combined`.
    """

    l2: float = 1.0
    max_iter: int = 100
    tol: float = 1e-8
    pairwise_weight: float = 1.0

    def fit(self, dataset: object) -> RewardModel:
        """Fit a pointwise reward model from director accept / reject labels."""

        ds = as_sample_dataset(dataset)
        feats = np.array([s.features for s in ds], dtype=Float)
        y = np.array([s.reward for s in ds], dtype=Float)
        sw = np.array([s.weight for s in ds], dtype=Float)
        # Binarize the soft labels for the logistic likelihood but fold the
        # distance-from-0.5 into the sample weight so an 'edit' (reward≈0.35) still
        # pulls toward reject, just more gently than a hard reject.
        yb = (y >= 0.5).astype(Float)
        confidence = np.abs(y - 0.5) * 2.0  # 0 at 0.5, 1 at the extremes
        eff_w = sw * np.clip(confidence, 0.05, 1.0)
        std = Standardizer.fit(feats)
        x = add_bias(std.transform(feats))
        fit = fit_logistic(
            x, yb, sample_weight=eff_w, l2=self.l2, max_iter=self.max_iter, tol=self.tol
        )
        return RewardModel(
            weights=fit.weights,
            standardizer=std,
            dim=feats.shape[1],
            converged=fit.converged,
            train_objective="pointwise",
        )

    def fit_pairwise(self, pairs: PreferenceDataset) -> RewardModel:
        """Fit a Bradley–Terry reward model from pairwise preferences alone.

        Models ``P(A ≻ B) = sigmoid(w·(φ(A) - φ(B)))``. With no bias term in the
        difference (it cancels), the absolute scale is unidentified, so the
        returned model's :meth:`reward` is monotone-meaningful but uncalibrated;
        use :meth:`fit_combined` when an accept-probability is needed.
        """

        if len(pairs) == 0:
            raise DataError("fit_pairwise needs at least one preference pair")
        diffs = np.array([p.diff() for p in pairs], dtype=Float)
        strengths = np.array([p.strength for p in pairs], dtype=Float)
        # Stack each pair both ways so the symmetric BT likelihood is balanced and
        # the bias coefficient is pinned to 0 (it cancels in a difference).
        x = np.vstack([diffs, -diffs])
        y = np.concatenate([np.ones(len(pairs)), np.zeros(len(pairs))]).astype(Float)
        sw = np.concatenate([strengths, strengths])
        x_design = add_bias(x)
        fit = fit_logistic(
            x_design,
            y,
            sample_weight=sw,
            l2=self.l2,
            max_iter=self.max_iter,
            tol=self.tol,
            fit_bias_l2=True,  # force the (meaningless) bias toward 0
        )
        # Identity standardizer: differences are already centered; we model raw φ.
        dim = diffs.shape[1]
        std = Standardizer(
            mean=np.zeros(dim, dtype=Float), scale=np.ones(dim, dtype=Float)
        )
        return RewardModel(
            weights=fit.weights,
            standardizer=std,
            dim=dim,
            converged=fit.converged,
            train_objective="pairwise",
        )

    def fit_combined(
        self, dataset: object, pairs: PreferenceDataset
    ) -> RewardModel:
        """Joint pointwise + pairwise fit — calibrated *and* well-ranked.

        Builds one logistic problem whose rows are the pointwise samples (anchor
        the absolute accept-scale) plus the symmetrized pairwise differences
        (sharpen the ranking, weighted by ``pairwise_weight``). Standardization is
        fit on the pointwise features and applied to both blocks so the single
        weight vector is coherent across them.
        """

        ds = as_sample_dataset(dataset)
        feats = np.array([s.features for s in ds], dtype=Float)
        std = Standardizer.fit(feats)
        # Pointwise block.
        xp = add_bias(std.transform(feats))
        yp = (np.array([s.reward for s in ds], dtype=Float) >= 0.5).astype(Float)
        wp = np.array([s.weight for s in ds], dtype=Float)
        # Pairwise block — standardize each side then difference (bias cancels).
        if len(pairs) > 0:
            win = std.transform(np.array([p.winner for p in pairs], dtype=Float))
            lose = std.transform(np.array([p.loser for p in pairs], dtype=Float))
            diffs = win - lose
            strengths = np.array([p.strength for p in pairs], dtype=Float)
            xd = np.vstack([diffs, -diffs])
            # No bias on difference rows.
            xd_design = np.hstack([np.zeros((xd.shape[0], 1), dtype=Float), xd])
            yd = np.concatenate([np.ones(len(pairs)), np.zeros(len(pairs))])
            wd = np.concatenate([strengths, strengths]) * float(self.pairwise_weight)
            x = np.vstack([xp, xd_design])
            y = np.concatenate([yp, yd]).astype(Float)
            sw = np.concatenate([wp, wd])
        else:
            x, y, sw = xp, yp, wp
        fit = fit_logistic(
            x, y, sample_weight=sw, l2=self.l2, max_iter=self.max_iter, tol=self.tol
        )
        return RewardModel(
            weights=fit.weights,
            standardizer=std,
            dim=feats.shape[1],
            converged=fit.converged,
            train_objective="combined",
        )


def _roc_auc(y: FloatArray, score: FloatArray) -> float:
    """ROC AUC via the rank-sum (Mann–Whitney U) identity, ties averaged.

    Returns 0.5 when one class is absent (AUC is undefined; 0.5 = chance).
    """

    y = np.asarray(y, dtype=Float).ravel()
    score = np.asarray(score, dtype=Float).ravel()
    pos = score[y >= 0.5]
    neg = score[y < 0.5]
    n_pos, n_neg = len(pos), len(neg)
    if n_pos == 0 or n_neg == 0:
        return 0.5
    order = np.argsort(score, kind="mergesort")
    ranks = np.empty(len(score), dtype=Float)
    sorted_scores = score[order]
    i = 0
    n = len(score)
    while i < n:
        j = i
        while j + 1 < n and sorted_scores[j + 1] == sorted_scores[i]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 1-based, ties share the mean rank
        ranks[order[i : j + 1]] = avg_rank
        i = j + 1
    rank_sum_pos = float(ranks[y >= 0.5].sum())
    auc = (rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
    return float(auc)


# Re-exported for callers that build samples ad hoc.
__all__ = [
    "RewardModel",
    "RewardModelTrainer",
    "RewardMetrics",
    "Sample",
    "PreferencePair",
]
