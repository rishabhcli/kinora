"""Learned-model audit report — confusion / Brier / ROC-AUC / threshold sweep."""

from __future__ import annotations

from app.render.qa.report import (
    brier_score,
    confusion_at,
    evaluate_reward,
    roc_auc,
    threshold_sweep,
)
from app.render.reward import QASample, fit_reward


def _preds_perfect() -> list[tuple[float, bool]]:
    # Reward perfectly agrees with the label.
    return [(0.95, True), (0.90, True), (0.10, False), (0.05, False)]


def _preds_mixed() -> list[tuple[float, bool]]:
    return [(0.95, True), (0.40, True), (0.60, False), (0.05, False)]


# --------------------------------------------------------------------------- #
# Confusion matrix + derived rates
# --------------------------------------------------------------------------- #


def test_confusion_perfect() -> None:
    cm = confusion_at(_preds_perfect())
    assert cm.true_pos == 2 and cm.true_neg == 2
    assert cm.false_pos == 0 and cm.false_neg == 0
    assert cm.accuracy == 1.0
    assert cm.precision == 1.0
    assert cm.recall == 1.0
    assert cm.f1 == 1.0


def test_confusion_mixed() -> None:
    cm = confusion_at(_preds_mixed())
    # 0.95→T (TP), 0.40→F but label T (FN), 0.60→T but label F (FP), 0.05→F (TN)
    assert cm.true_pos == 1
    assert cm.false_neg == 1
    assert cm.false_pos == 1
    assert cm.true_neg == 1
    assert cm.accuracy == 0.5


# --------------------------------------------------------------------------- #
# Brier score
# --------------------------------------------------------------------------- #


def test_brier_perfect_is_near_zero() -> None:
    assert brier_score([(1.0, True), (0.0, False)]) == 0.0


def test_brier_worst_is_near_one() -> None:
    assert brier_score([(0.0, True), (1.0, False)]) == 1.0


def test_brier_empty() -> None:
    assert brier_score([]) == 0.0


# --------------------------------------------------------------------------- #
# ROC-AUC (threshold-free ranking)
# --------------------------------------------------------------------------- #


def test_roc_auc_perfect_separation() -> None:
    assert roc_auc(_preds_perfect()) == 1.0


def test_roc_auc_reversed_is_zero() -> None:
    # Rewards perfectly anti-correlated with the label.
    assert roc_auc([(0.1, True), (0.9, False)]) == 0.0


def test_roc_auc_one_class_is_chance() -> None:
    assert roc_auc([(0.9, True), (0.8, True)]) == 0.5


# --------------------------------------------------------------------------- #
# Threshold sweep
# --------------------------------------------------------------------------- #


def test_threshold_sweep_covers_cuts() -> None:
    sweep = threshold_sweep(_preds_mixed(), cuts=(0.3, 0.5, 0.7))
    assert [p.threshold for p in sweep] == [0.3, 0.5, 0.7]
    # recall is non-increasing as the cut rises (fewer predicted positives)
    recalls = [p.recall for p in sweep]
    assert recalls == sorted(recalls, reverse=True)


# --------------------------------------------------------------------------- #
# End-to-end: evaluate a fitted reward
# --------------------------------------------------------------------------- #


def test_evaluate_reward_on_separable_data() -> None:
    samples = [
        QASample(0.93, 0.02, True, 0.05, accepted=True) for _ in range(20)
    ] + [
        QASample(0.55, 0.30, True, 0.60, accepted=False) for _ in range(20)
    ]
    weights = fit_reward(samples)
    report = evaluate_reward(weights, samples)
    assert report.n == 40
    assert report.roc_auc > 0.9  # cleanly ranks accepts above rejects
    assert report.confusion.accuracy > 0.9
    assert report.brier < 0.2  # well-calibrated magnitudes
    assert len(report.sweep) == 7  # default cuts
