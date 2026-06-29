"""Deterministic numerical primitives shared by the alignment learners.

Pure NumPy, no randomness anywhere unless a caller passes an explicit seed, and
no I/O. Every routine here is exhaustively unit-tested for correctness and
convergence so the higher-level reward / DPO / Bradley–Terry models can rely on
it as a trusted base layer.

Notable choices:

* :func:`sigmoid` is the numerically-stable two-branch logistic (no ``exp``
  overflow for large-magnitude logits).
* :func:`fit_logistic` is L2-regularized maximum-likelihood logistic regression
  solved by **IRLS / Newton** (the canonical second-order method) with a damped
  fallback, so it converges quadratically on well-conditioned problems and never
  diverges on separable ones (the ridge term guarantees a finite optimum).
* :class:`Standardizer` is a fit/transform z-scorer with a stored mean / scale so
  a model trained on standardized features can score raw vectors later.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

from .errors import ConvergenceError, DataError

Float = np.float64
FloatArray = NDArray[np.float64]
#: Anything ``np.asarray`` accepts — a feature vector may arrive as a list, tuple,
#: or ndarray. Public scoring methods accept this and normalize internally.
Features = ArrayLike

#: A tiny floor that keeps logs / divisions finite without perturbing results.
EPS = 1e-12


def sigmoid(z: FloatArray | float) -> FloatArray:
    """Numerically stable logistic ``1 / (1 + e^{-z})``.

    Uses the two-branch form so neither ``e^{-z}`` (large positive ``z``) nor
    ``e^{z}`` (large negative ``z``) overflows.
    """

    z_arr = np.asarray(z, dtype=Float)
    out = np.empty_like(z_arr)
    pos = z_arr >= 0
    neg = ~pos
    out[pos] = 1.0 / (1.0 + np.exp(-z_arr[pos]))
    exp_z = np.exp(z_arr[neg])
    out[neg] = exp_z / (1.0 + exp_z)
    return out


def log_sigmoid(z: FloatArray | float) -> FloatArray:
    """Stable ``log sigmoid(z)`` via ``-softplus(-z)`` (no overflow either side)."""

    z_arr = np.asarray(z, dtype=Float)
    return -softplus(-z_arr)


def softplus(z: FloatArray | float) -> FloatArray:
    """Stable ``log(1 + e^z)`` = ``max(z, 0) + log1p(e^{-|z|})``."""

    z_arr = np.asarray(z, dtype=Float)
    return np.maximum(z_arr, 0.0) + np.log1p(np.exp(-np.abs(z_arr)))


def add_bias(x: FloatArray) -> FloatArray:
    """Prepend a column of ones so the bias term rides in the weight vector."""

    x = np.atleast_2d(np.asarray(x, dtype=Float))
    return np.hstack([np.ones((x.shape[0], 1), dtype=Float), x])


@dataclass(frozen=True)
class LogisticFit:
    """The result of :func:`fit_logistic`: the weight vector + diagnostics.

    ``weights[0]`` is the bias when the design matrix was built with
    :func:`add_bias`. ``converged`` / ``iterations`` / ``loss`` let the caller
    audit the fit; ``grad_norm`` is the final gradient infinity-norm.
    """

    weights: FloatArray
    converged: bool
    iterations: int
    loss: float
    grad_norm: float

    def predict_proba(self, x_design: FloatArray) -> FloatArray:
        """``P(y=1)`` for a design matrix already shaped like the training one."""

        z = np.asarray(x_design, dtype=Float) @ self.weights
        return sigmoid(z)


def fit_logistic(
    x_design: FloatArray,
    y: FloatArray,
    *,
    sample_weight: FloatArray | None = None,
    l2: float = 1.0,
    max_iter: int = 100,
    tol: float = 1e-8,
    fit_bias_l2: bool = False,
    strict: bool = False,
) -> LogisticFit:
    """L2-regularized logistic regression by IRLS / damped Newton.

    Minimizes the weighted negative log-likelihood plus ``(l2/2) * ||w||^2``
    (excluding the bias term unless ``fit_bias_l2``). The Hessian is
    ``XᵀWX + l2·I`` which is positive-definite for ``l2 > 0``, so the Newton step
    is always a descent direction; a halving line search guarantees monotone loss
    decrease even when the quadratic model overshoots.

    Returns a :class:`LogisticFit`. If ``strict`` and the gradient norm is still
    above ``tol`` at ``max_iter``, raises :class:`ConvergenceError`.
    """

    x = np.atleast_2d(np.asarray(x_design, dtype=Float))
    y = np.asarray(y, dtype=Float).ravel()
    n, d = x.shape
    if y.shape[0] != n:
        raise DataError(f"X has {n} rows but y has {y.shape[0]}")
    if l2 < 0:
        raise DataError(f"l2 must be >= 0, got {l2}")
    if sample_weight is None:
        sw = np.ones(n, dtype=Float)
    else:
        sw = np.asarray(sample_weight, dtype=Float).ravel()
        if sw.shape[0] != n:
            raise DataError(f"sample_weight has {sw.shape[0]} entries, expected {n}")
        if np.any(sw < 0):
            raise DataError("sample_weight must be non-negative")
    sw_sum = float(sw.sum())
    if sw_sum <= 0:
        raise DataError("sample_weight sums to zero")

    # Ridge mask: regularize every coefficient, optionally skipping the bias.
    reg = np.full(d, float(l2), dtype=Float)
    if not fit_bias_l2 and d >= 1:
        reg[0] = 0.0

    w = np.zeros(d, dtype=Float)

    def _loss(weights: FloatArray) -> float:
        z = x @ weights
        # weighted NLL: -sum sw*(y*log p + (1-y)*log(1-p)) = sum sw*(softplus(z) - y*z)
        nll = float(np.sum(sw * (softplus(z) - y * z)))
        ridge = 0.5 * float(np.sum(reg * weights * weights))
        return (nll + ridge) / sw_sum

    loss = _loss(w)
    grad_norm = float("inf")
    converged = False
    it = 0
    for it in range(1, max_iter + 1):  # noqa: B007 - `it` is the reported iter count
        p = sigmoid(x @ w)
        grad = x.T @ (sw * (p - y)) + reg * w
        grad_norm = float(np.max(np.abs(grad)))
        if grad_norm <= tol:
            converged = True
            break
        # IRLS weights, floored so the Hessian stays well-conditioned.
        s = np.clip(p * (1.0 - p), 1e-9, None) * sw
        hess = (x.T * s) @ x + np.diag(reg)
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:  # pragma: no cover - ridge keeps it PD
            step = np.linalg.lstsq(hess, grad, rcond=None)[0]
        # Damped Newton: halve the step until the loss does not increase.
        alpha = 1.0
        new_loss = loss
        for _ in range(40):
            candidate = w - alpha * step
            new_loss = _loss(candidate)
            if new_loss <= loss + 1e-12:
                break
            alpha *= 0.5
        w = w - alpha * step
        if abs(loss - new_loss) <= tol * (1.0 + abs(loss)):
            loss = new_loss
            # Recompute the gradient at the new point for an honest report.
            p = sigmoid(x @ w)
            grad = x.T @ (sw * (p - y)) + reg * w
            grad_norm = float(np.max(np.abs(grad)))
            converged = grad_norm <= max(tol, 1e-6)
            break
        loss = new_loss

    if strict and not converged:
        raise ConvergenceError(
            f"logistic fit did not converge in {max_iter} iters (||g||={grad_norm:.2e})"
        )
    return LogisticFit(
        weights=w, converged=converged, iterations=it, loss=loss, grad_norm=grad_norm
    )


@dataclass(frozen=True)
class Standardizer:
    """Z-score features to zero mean / unit variance with a stored transform.

    Columns with (near-)zero variance keep scale 1 so they pass through unchanged
    instead of exploding. ``fit`` returns a new immutable instance.
    """

    mean: FloatArray
    scale: FloatArray

    @classmethod
    def fit(cls, x: FloatArray) -> Standardizer:
        x = np.atleast_2d(np.asarray(x, dtype=Float))
        mean = x.mean(axis=0)
        std = x.std(axis=0)
        scale = np.where(std < 1e-9, 1.0, std)
        return cls(mean=mean.astype(Float), scale=scale.astype(Float))

    def transform(self, x: FloatArray) -> FloatArray:
        x = np.atleast_2d(np.asarray(x, dtype=Float))
        return (x - self.mean) / self.scale


def expected_calibration_error(
    proba: FloatArray, y: FloatArray, *, n_bins: int = 10
) -> float:
    """Expected Calibration Error: weighted gap between confidence and accuracy.

    Bins predictions into ``n_bins`` equal-width confidence buckets and returns
    ``sum_b (|B_b|/n) * |acc(B_b) - conf(B_b)|``. 0 = perfectly calibrated.
    """

    proba = np.asarray(proba, dtype=Float).ravel()
    y = np.asarray(y, dtype=Float).ravel()
    if proba.shape != y.shape:
        raise DataError("proba and y must share shape")
    if n_bins < 1:
        raise DataError("n_bins must be >= 1")
    n = proba.shape[0]
    if n == 0:
        raise DataError("cannot compute ECE on empty input")
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    for b in range(n_bins):
        lo, hi = edges[b], edges[b + 1]
        in_bin = (proba > lo) & (proba <= hi) if b > 0 else (proba >= lo) & (proba <= hi)
        cnt = int(in_bin.sum())
        if cnt == 0:
            continue
        conf = float(proba[in_bin].mean())
        acc = float(y[in_bin].mean())
        ece += (cnt / n) * abs(acc - conf)
    return ece
