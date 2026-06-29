"""Correctness + convergence tests for the alignment numerical primitives."""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.mlplatform.alignment.errors import ConvergenceError, DataError
from app.mlplatform.alignment.linalg import (
    Standardizer,
    add_bias,
    expected_calibration_error,
    fit_logistic,
    log_sigmoid,
    sigmoid,
    softplus,
)


def test_sigmoid_stable_at_extremes() -> None:
    assert sigmoid(0.0)[()] == pytest.approx(0.5)
    # No overflow for large-magnitude logits.
    assert sigmoid(1000.0)[()] == pytest.approx(1.0)
    assert sigmoid(-1000.0)[()] == pytest.approx(0.0)
    z = np.array([-50.0, 0.0, 50.0])
    out = sigmoid(z)
    assert np.all(np.isfinite(out))
    assert np.all((out >= 0) & (out <= 1))


def test_log_sigmoid_and_softplus_identities() -> None:
    z = np.array([-30.0, -1.0, 0.0, 1.0, 30.0])
    # log sigmoid(z) == -softplus(-z)
    np.testing.assert_allclose(log_sigmoid(z), -softplus(-z), atol=1e-12)
    # softplus is finite and >= max(z, 0)
    sp = softplus(z)
    assert np.all(np.isfinite(sp))
    assert np.all(sp >= np.maximum(z, 0.0) - 1e-9)


def test_fit_logistic_recovers_known_separator() -> None:
    # Build a clean, linearly separable-ish problem with a known boundary.
    rng = np.random.default_rng(0)
    n = 400
    x = rng.normal(size=(n, 2))
    true_w = np.array([0.0, 2.5, -1.5])  # bias, w1, w2
    logits = add_bias(x) @ true_w
    proba = 1.0 / (1.0 + np.exp(-logits))
    y = (proba >= 0.5).astype(float)
    fit = fit_logistic(add_bias(x), y, l2=1e-3, max_iter=200)
    assert fit.converged
    # Direction of the recovered weights matches the truth (cosine > 0.9).
    w = fit.weights[1:]
    cos = float(w @ true_w[1:] / (np.linalg.norm(w) * np.linalg.norm(true_w[1:])))
    assert cos > 0.95


def test_fit_logistic_monotone_loss_and_converges() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(size=(120, 3))
    y = (x[:, 0] + 0.5 * x[:, 1] - x[:, 2] > 0).astype(float)
    fit = fit_logistic(add_bias(x), y, l2=1.0, max_iter=100)
    assert fit.converged
    assert fit.grad_norm < 1e-5
    # Loss is the regularized NLL — strictly positive and finite.
    assert math.isfinite(fit.loss) and fit.loss > 0


def test_fit_logistic_strict_raises_on_nonconvergence() -> None:
    x = np.array([[0.0], [1.0]])
    y = np.array([0.0, 1.0])
    with pytest.raises(ConvergenceError):
        # 0 iterations can never converge; strict must raise.
        fit_logistic(add_bias(x), y, l2=0.0, max_iter=0, strict=True)


def test_fit_logistic_separable_stays_finite_with_ridge() -> None:
    # Perfectly separable data: without ridge weights diverge; ridge bounds them.
    x = np.array([[-2.0], [-1.0], [1.0], [2.0]])
    y = np.array([0.0, 0.0, 1.0, 1.0])
    fit = fit_logistic(add_bias(x), y, l2=1.0, max_iter=200)
    assert np.all(np.isfinite(fit.weights))
    assert fit.converged


def test_fit_logistic_sample_weight_shifts_fit() -> None:
    x = np.array([[1.0], [1.0], [-1.0]])
    y = np.array([1.0, 1.0, 0.0])
    base = fit_logistic(add_bias(x), y, l2=0.1, max_iter=200)
    # Up-weight the negative example heavily; the boundary should move.
    weighted = fit_logistic(
        add_bias(x), y, sample_weight=np.array([1.0, 1.0, 50.0]), l2=0.1, max_iter=200
    )
    assert not np.allclose(base.weights, weighted.weights)


def test_fit_logistic_rejects_bad_inputs() -> None:
    with pytest.raises(DataError):
        fit_logistic(np.zeros((3, 2)), np.zeros(2))  # row mismatch
    with pytest.raises(DataError):
        fit_logistic(np.zeros((2, 1)), np.zeros(2), l2=-1.0)
    with pytest.raises(DataError):
        fit_logistic(
            np.zeros((2, 1)), np.zeros(2), sample_weight=np.array([-1.0, 1.0])
        )


def test_standardizer_roundtrip_and_zero_variance() -> None:
    x = np.array([[1.0, 5.0], [3.0, 5.0], [5.0, 5.0]])  # col 1 is constant
    std = Standardizer.fit(x)
    z = std.transform(x)
    # Column 0 standardized to zero mean.
    assert z[:, 0].mean() == pytest.approx(0.0, abs=1e-9)
    # Constant column passes through unchanged (scale forced to 1).
    assert std.scale[1] == pytest.approx(1.0)
    assert np.all(np.isfinite(z))


def test_expected_calibration_error_perfect_and_bad() -> None:
    # Perfectly calibrated: predict the true rate in each homogeneous bin.
    proba = np.array([0.0, 0.0, 1.0, 1.0])
    y = np.array([0.0, 0.0, 1.0, 1.0])
    assert expected_calibration_error(proba, y) == pytest.approx(0.0, abs=1e-9)
    # Maximally miscalibrated: confident-wrong everywhere.
    proba_bad = np.array([1.0, 1.0, 0.0, 0.0])
    assert expected_calibration_error(proba_bad, y) == pytest.approx(1.0, abs=1e-9)


def test_ece_rejects_bad_shapes() -> None:
    with pytest.raises(DataError):
        expected_calibration_error(np.zeros(3), np.zeros(2))
    with pytest.raises(DataError):
        expected_calibration_error(np.zeros(0), np.zeros(0))
