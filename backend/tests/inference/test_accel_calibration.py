"""Calibration tests — deterministic threshold selection from labelled pairs."""

from __future__ import annotations

import pytest

from app.inference.accel.calibration import (
    LabeledPair,
    calibrate_threshold,
    candidate_thresholds,
    evaluate_threshold,
)
from app.inference.accel.errors import CalibrationError


def _pairs() -> list[LabeledPair]:
    # Clean separation: equivalents score high, different score low, with one
    # ambiguous equivalent at 0.90 that a precision target may sacrifice.
    return [
        LabeledPair(0.99, True),
        LabeledPair(0.97, True),
        LabeledPair(0.95, True),
        LabeledPair(0.90, True),  # borderline equivalent
        LabeledPair(0.88, False),  # borderline different
        LabeledPair(0.70, False),
        LabeledPair(0.40, False),
    ]


def test_perfect_separation_threshold() -> None:
    pairs = [
        LabeledPair(0.99, True),
        LabeledPair(0.96, True),
        LabeledPair(0.80, False),
        LabeledPair(0.50, False),
    ]
    res = calibrate_threshold(pairs, target_precision=1.0)
    assert res.precision == 1.0
    assert res.recall == 1.0
    assert 0.80 < res.threshold < 0.96


def test_precision_target_trades_recall() -> None:
    pairs = _pairs()
    # Demand perfect precision: must exclude the 0.88 false, so threshold lands
    # above it but can still keep all four equivalents (lowest equiv is 0.90).
    res = calibrate_threshold(pairs, target_precision=1.0)
    assert res.false_positives == 0
    assert res.precision == 1.0
    assert 0.88 < res.threshold <= 0.90
    assert res.recall == 1.0  # all four equivalents kept


def test_lower_precision_allows_more_recall_but_includes_fp() -> None:
    pairs = [
        LabeledPair(0.95, True),
        LabeledPair(0.93, False),  # a false positive if we go low enough
        LabeledPair(0.91, True),
    ]
    # 1.0 precision must exclude the 0.93 false -> threshold just above it,
    # which also excludes the 0.91 true (recall drops).
    strict = calibrate_threshold(pairs, target_precision=1.0)
    assert strict.false_positives == 0
    assert strict.recall < 1.0
    # A looser precision target admits the lower threshold, regaining recall.
    loose = calibrate_threshold(pairs, target_precision=0.6)
    assert loose.recall >= strict.recall


def test_min_recall_constraint() -> None:
    pairs = _pairs()
    res = calibrate_threshold(pairs, target_precision=0.5, min_recall=1.0)
    assert res.recall == 1.0


def test_unsatisfiable_targets_raise() -> None:
    # Identical score, opposite labels: any threshold that accepts the True one
    # also accepts the False one -> max precision while admitting a positive is
    # 0.5. Demanding high precision AND non-zero recall is impossible (rejecting
    # everything would give vacuous precision but zero recall, which min_recall
    # rules out).
    pairs = [
        LabeledPair(0.9, True),
        LabeledPair(0.9, False),
    ]
    with pytest.raises(CalibrationError):
        calibrate_threshold(pairs, target_precision=0.99, min_recall=0.5)


def test_empty_set_raises() -> None:
    with pytest.raises(CalibrationError):
        calibrate_threshold([])
    with pytest.raises(CalibrationError):
        evaluate_threshold([], 0.5)


def test_invalid_precision_raises() -> None:
    with pytest.raises(CalibrationError):
        calibrate_threshold(_pairs(), target_precision=1.5)


def test_evaluate_threshold_matches_confusion() -> None:
    pairs = _pairs()
    res = evaluate_threshold(pairs, 0.92)
    # >= 0.92: 0.99,0.97,0.95 (all True) accepted -> tp=3; 0.90 True rejected -> fn=1
    assert res.true_positives == 3
    assert res.false_negatives == 1
    assert res.false_positives == 0
    assert res.precision == 1.0
    assert res.recall == pytest.approx(0.75)


def test_candidate_thresholds_cover_distinct_decisions() -> None:
    pairs = [LabeledPair(0.5, True), LabeledPair(0.7, False), LabeledPair(0.9, True)]
    cands = candidate_thresholds(pairs)
    # one below min, midpoints between sorted scores, one above max
    assert len(cands) == 4
    assert cands[0] < 0.5
    assert cands[-1] > 0.9
    assert all(a < b for a, b in zip(cands, cands[1:], strict=False))


def test_calibration_is_deterministic() -> None:
    pairs = _pairs()
    a = calibrate_threshold(pairs, target_precision=0.9)
    b = calibrate_threshold(pairs, target_precision=0.9)
    assert a == b
