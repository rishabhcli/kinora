"""Tests for post-hoc reward calibration (Platt + isotonic) and diagnostics."""

from __future__ import annotations

import numpy as np
import pytest

from app.mlplatform.alignment.calibration import (
    IsotonicCalibrator,
    PlattCalibrator,
    reliability_curve,
)
from app.mlplatform.alignment.errors import DataError


def _miscalibrated(n: int = 2000, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Scores whose true accept-prob is a logistic of the score, but the raw
    score is *not* itself a probability (scale + shift), so calibration is needed.
    """

    rng = np.random.default_rng(seed)
    score = rng.uniform(-3, 3, size=n)
    true_p = 1.0 / (1.0 + np.exp(-(0.8 * score - 0.5)))
    y = (rng.uniform(size=n) < true_p).astype(float)
    return score, y


def test_platt_improves_calibration() -> None:
    score, y = _miscalibrated()
    # Treating the raw score (range ~[-3,3]) as a probability is badly calibrated.
    raw_as_prob = np.clip((score + 3) / 6, 0, 1)
    before = reliability_curve(raw_as_prob, y).ece
    cal = PlattCalibrator.fit(score, y)
    after = reliability_curve(cal.transform(score), y).ece
    assert after < before
    assert after < 0.05
    # Positive association recovered.
    assert cal.a > 0


def test_platt_outputs_are_probabilities() -> None:
    score, y = _miscalibrated(seed=1)
    cal = PlattCalibrator.fit(score, y)
    p = cal.transform(score)
    assert np.all((p >= 0) & (p <= 1))
    # Monotone in the score (Platt is a monotone logistic in a>0).
    grid = np.array([-2.0, -1.0, 0.0, 1.0, 2.0])
    pg = cal.transform(grid)
    assert np.all(np.diff(pg) >= -1e-9)


def test_isotonic_is_monotone_and_calibrated() -> None:
    score, y = _miscalibrated(seed=2)
    cal = IsotonicCalibrator.fit(score, y)
    p = cal.transform(score)
    assert np.all((p >= 0) & (p <= 1))
    # Monotone non-decreasing over a sorted grid.
    grid = np.linspace(-3, 3, 50)
    pg = cal.transform(grid)
    assert np.all(np.diff(pg) >= -1e-9)
    assert reliability_curve(p, y).ece < 0.05


def test_isotonic_recovers_step_function() -> None:
    # A perfectly separable step: scores < 0 reject, scores > 0 accept.
    score = np.array([-2.0, -1.0, -0.5, 0.5, 1.0, 2.0])
    y = np.array([0.0, 0.0, 0.0, 1.0, 1.0, 1.0])
    cal = IsotonicCalibrator.fit(score, y)
    assert cal.transform([-1.5])[0] == pytest.approx(0.0, abs=1e-9)
    assert cal.transform([1.5])[0] == pytest.approx(1.0, abs=1e-9)


def test_isotonic_pools_violators() -> None:
    # Non-monotone raw labels must be pooled into a monotone fit.
    score = np.array([1.0, 2.0, 3.0, 4.0])
    y = np.array([0.0, 1.0, 0.0, 1.0])  # violation at 2->3
    cal = IsotonicCalibrator.fit(score, y)
    pg = cal.transform(score)
    assert np.all(np.diff(pg) >= -1e-9)


def test_calibrators_roundtrip() -> None:
    score, y = _miscalibrated(seed=3)
    platt = PlattCalibrator.fit(score, y)
    iso = IsotonicCalibrator.fit(score, y)
    platt2 = PlattCalibrator.from_dict(platt.to_dict())
    iso2 = IsotonicCalibrator.from_dict(iso.to_dict())
    np.testing.assert_allclose(platt.transform(score), platt2.transform(score))
    np.testing.assert_allclose(iso.transform(score), iso2.transform(score))


def test_reliability_curve_perfect_and_brier() -> None:
    proba = np.array([0.0, 0.0, 1.0, 1.0])
    y = np.array([0.0, 0.0, 1.0, 1.0])
    diag = reliability_curve(proba, y)
    assert diag.ece == pytest.approx(0.0, abs=1e-9)
    assert diag.brier == pytest.approx(0.0, abs=1e-9)
    assert sum(diag.bin_count) == 4


def test_calibration_rejects_bad_input() -> None:
    with pytest.raises(DataError):
        PlattCalibrator.fit(np.array([1.0, 2.0]), np.array([1.0]))
    with pytest.raises(DataError):
        IsotonicCalibrator.fit(np.array([]), np.array([]))
    with pytest.raises(DataError):
        reliability_curve(np.array([0.5]), np.array([1.0, 0.0]))
    with pytest.raises(DataError):
        PlattCalibrator.fit(np.array([np.inf, 1.0]), np.array([1.0, 0.0]))
