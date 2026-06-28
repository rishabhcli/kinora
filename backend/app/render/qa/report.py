"""Learned-model evaluation + audit report (§13 honesty, §9.5 self-improvement).

A learned reward is only trustworthy if you can *measure* how well it agrees with the
director's actual accept/reject decisions — and §13 insists every number be honest and
pre-registered, not tuned to flatter. This module evaluates a fitted reward against a
held-out labeled set and produces an auditable report:

* a **confusion matrix** of the reward (thresholded at 0.5) vs the director label, and
  the precision / recall / F1 / accuracy that fall out of it;
* the **Brier score** (mean squared error of the probability vs the 0/1 label) — the
  proper scoring rule for whether the reward's *magnitude* is calibrated, not just its
  ranking;
* **ROC-AUC** via the Mann–Whitney statistic — whether the reward *ranks* accepts
  above rejects regardless of any threshold;
* a **threshold sweep** so an operator can pick the review-floor with eyes open
  (precision/recall at each candidate cut) instead of guessing a magic number.

Everything is pure over already-computed ``(reward, accepted)`` pairs, so the audit
runs offline alongside the calibration pass with no model call and no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.render.reward import QASample, RewardWeights, reward_of


@dataclass(frozen=True, slots=True)
class ConfusionMatrix:
    """Reward (thresholded) vs the director label — the 2×2 of agreement."""

    true_pos: int = 0
    false_pos: int = 0
    true_neg: int = 0
    false_neg: int = 0

    @property
    def total(self) -> int:
        return self.true_pos + self.false_pos + self.true_neg + self.false_neg

    @property
    def accuracy(self) -> float:
        return (self.true_pos + self.true_neg) / self.total if self.total else 1.0

    @property
    def precision(self) -> float:
        denom = self.true_pos + self.false_pos
        return self.true_pos / denom if denom else 1.0

    @property
    def recall(self) -> float:
        denom = self.true_pos + self.false_neg
        return self.true_pos / denom if denom else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass(frozen=True, slots=True)
class ThresholdPoint:
    """Precision / recall at one candidate review-floor."""

    threshold: float
    precision: float
    recall: float
    f1: float


@dataclass(frozen=True, slots=True)
class RewardReport:
    """The full audit of a fitted reward against a labeled set."""

    n: int = 0
    confusion: ConfusionMatrix = field(default_factory=ConfusionMatrix)
    brier: float = 0.0
    roc_auc: float = 0.5
    sweep: list[ThresholdPoint] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return round(self.confusion.accuracy, 4)


def confusion_at(
    predictions: Sequence[tuple[float, bool]], *, threshold: float = 0.5
) -> ConfusionMatrix:
    """The confusion matrix of ``reward >= threshold`` vs the director label."""
    tp = fp = tn = fn = 0
    for reward, accepted in predictions:
        predicted = reward >= threshold
        if predicted and accepted:
            tp += 1
        elif predicted and not accepted:
            fp += 1
        elif not predicted and not accepted:
            tn += 1
        else:
            fn += 1
    return ConfusionMatrix(true_pos=tp, false_pos=fp, true_neg=tn, false_neg=fn)


def brier_score(predictions: Sequence[tuple[float, bool]]) -> float:
    """Mean squared error of the reward vs the 0/1 label (lower = better calibrated)."""
    if not predictions:
        return 0.0
    total = sum((reward - (1.0 if acc else 0.0)) ** 2 for reward, acc in predictions)
    return round(total / len(predictions), 6)


def roc_auc(predictions: Sequence[tuple[float, bool]]) -> float:
    """ROC-AUC via the Mann–Whitney U statistic (ties count as half).

    ``P(reward(accept) > reward(reject))`` — threshold-free separation. Returns 0.5
    (chance) when either class is empty.
    """
    pos = [r for r, a in predictions if a]
    neg = [r for r, a in predictions if not a]
    if not pos or not neg:
        return 0.5
    wins = 0.0
    for p in pos:
        for n in neg:
            if p > n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return round(wins / (len(pos) * len(neg)), 6)


def threshold_sweep(
    predictions: Sequence[tuple[float, bool]],
    *,
    cuts: Sequence[float] = (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8),
) -> list[ThresholdPoint]:
    """Precision/recall/F1 at each candidate review-floor (pick the cut with eyes open)."""
    points: list[ThresholdPoint] = []
    for cut in cuts:
        cm = confusion_at(predictions, threshold=cut)
        points.append(
            ThresholdPoint(
                threshold=cut,
                precision=round(cm.precision, 4),
                recall=round(cm.recall, 4),
                f1=round(cm.f1, 4),
            )
        )
    return points


def evaluate_reward(
    weights: RewardWeights,
    samples: Sequence[QASample],
    *,
    threshold: float = 0.5,
) -> RewardReport:
    """Audit a fitted reward against a labeled held-out set (pure).

    Scores each sample with the reward, then builds the confusion matrix, Brier score,
    ROC-AUC, and threshold sweep — the report an operator reads to decide whether to
    trust (and where to threshold) the learned layer.
    """
    predictions = [
        (
            reward_of(
                weights,
                ccs=s.ccs,
                style_drift=s.style_drift,
                timeline_ok=s.timeline_ok,
                motion_artifact=s.motion_artifact,
                aesthetic=s.aesthetic,
                temporal=s.temporal,
            ),
            s.accepted,
        )
        for s in samples
    ]
    return RewardReport(
        n=len(samples),
        confusion=confusion_at(predictions, threshold=threshold),
        brier=brier_score(predictions),
        roc_auc=roc_auc(predictions),
        sweep=threshold_sweep(predictions),
    )


__all__ = [
    "ConfusionMatrix",
    "RewardReport",
    "ThresholdPoint",
    "brier_score",
    "confusion_at",
    "evaluate_reward",
    "roc_auc",
    "threshold_sweep",
]
