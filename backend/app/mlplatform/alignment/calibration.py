"""Post-hoc probability calibration for the reward model (the "calibrated" in §13).

A reward model's raw output is a *score*; a calibrated reward is a *probability* a
downstream gate can threshold honestly ("accept when P(director accepts) ≥ τ").
Logistic regression is already fairly calibrated, but a learned reward over a
shifting director population drifts, so this module recalibrates *any* scorer's
output against held-out accept / reject labels with two standard, complementary
methods:

* **Platt scaling** (:class:`PlattCalibrator`) — fit a 1-D logistic ``σ(a·s + b)``
  mapping raw scores to probabilities. Parametric, robust on little data, assumes a
  sigmoidal score↔prob relationship.
* **Isotonic regression** (:class:`IsotonicCalibrator`) — the non-parametric
  monotone fit via the Pool-Adjacent-Violators Algorithm (PAVA). Makes no shape
  assumption beyond monotonicity, so it corrects arbitrary miscalibration given
  enough data; piecewise-constant with linear interpolation between knots.

Both expose ``transform`` (raw score → calibrated probability) and round-trip for
experiment tracking. :func:`reliability_curve` + :class:`CalibrationDiagnostics`
quantify the improvement (ECE, Brier score, the reliability diagram bins).

Pure NumPy, deterministic, fully unit-tested — calibration is exactly the kind of
"silently passes a clip the gate would fail" risk §13 warns about, so its
correctness is asserted, not assumed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import DataError, NotFittedError
from .linalg import (
    Features,
    Float,
    FloatArray,
    add_bias,
    expected_calibration_error,
    fit_logistic,
    sigmoid,
)


def _validate_scores_labels(scores: Features, labels: Features) -> tuple[FloatArray, FloatArray]:
    s = np.asarray(scores, dtype=Float).ravel()
    y = np.asarray(labels, dtype=Float).ravel()
    if s.shape != y.shape:
        raise DataError(f"scores {s.shape} and labels {y.shape} must match")
    if s.size == 0:
        raise DataError("cannot calibrate on empty input")
    if np.any(~np.isfinite(s)):
        raise DataError("scores contain non-finite values")
    yb = (y >= 0.5).astype(Float)
    return s, yb


@dataclass(frozen=True)
class PlattCalibrator:
    """Logistic recalibration ``P = σ(a·score + b)`` (Platt, 1999).

    Fit by the same regularized logistic solver as the reward model, so it inherits
    the convergence guarantees. ``a`` should come out positive when the score is
    positively associated with acceptance.
    """

    a: float
    b: float

    @classmethod
    def fit(cls, scores: Features, labels: Features, *, l2: float = 1e-6) -> PlattCalibrator:
        s, yb = _validate_scores_labels(scores, labels)
        design = add_bias(s.reshape(-1, 1))
        fit = fit_logistic(design, yb, l2=l2, max_iter=200)
        return cls(a=float(fit.weights[1]), b=float(fit.weights[0]))

    def transform(self, scores: Features) -> FloatArray:
        s = np.asarray(scores, dtype=Float).ravel()
        return sigmoid(self.a * s + self.b)

    def to_dict(self) -> dict[str, float]:
        return {"a": self.a, "b": self.b}

    @classmethod
    def from_dict(cls, d: dict[str, float]) -> PlattCalibrator:
        return cls(a=float(d["a"]), b=float(d["b"]))


@dataclass(frozen=True)
class IsotonicCalibrator:
    """Non-parametric monotone calibration via PAVA.

    Stores the fitted step function as sorted knot ``x`` (unique input scores) and
    ``y`` (the pooled monotone probabilities). ``transform`` clamps to the fitted
    range then linearly interpolates between knots, so it is monotone and
    continuous on the interior.
    """

    x: FloatArray
    y: FloatArray

    @classmethod
    def fit(cls, scores: Features, labels: Features) -> IsotonicCalibrator:
        s, yb = _validate_scores_labels(scores, labels)
        order = np.argsort(s, kind="mergesort")
        xs = s[order]
        ys = yb[order]
        # PAVA via a monotone stack of blocks, each tracking (sum, weight, value).
        # O(n): push every point, then pool any adjacent pair that violates
        # monotonicity (left value > right value) until the stack is non-decreasing.
        stack_val: list[float] = []
        stack_sum: list[float] = []
        stack_w: list[float] = []
        for k in range(len(ys)):
            stack_val.append(float(ys[k]))
            stack_sum.append(float(ys[k]))
            stack_w.append(1.0)
            while len(stack_val) > 1 and stack_val[-2] > stack_val[-1]:
                s2 = stack_sum.pop()
                w2 = stack_w.pop()
                stack_val.pop()
                s1 = stack_sum.pop()
                w1 = stack_w.pop()
                stack_val.pop()
                merged_w = w1 + w2
                merged_sum = s1 + s2
                stack_sum.append(merged_sum)
                stack_w.append(merged_w)
                stack_val.append(merged_sum / merged_w)
        # Expand block values back over the sorted points.
        fitted = np.empty(len(ys), dtype=Float)
        idx = 0
        for val, w in zip(stack_val, stack_w, strict=True):
            count = int(round(w))
            fitted[idx : idx + count] = val
            idx += count
        # Collapse to unique x knots (keep the last fitted value per x).
        uniq_x, inv = np.unique(xs, return_index=False, return_inverse=True)
        knot_y = np.empty(len(uniq_x), dtype=Float)
        for j in range(len(uniq_x)):
            knot_y[j] = fitted[inv == j].mean()
        return cls(x=uniq_x, y=knot_y)

    def transform(self, scores: Features) -> FloatArray:
        if self.x.size == 0:
            raise NotFittedError("IsotonicCalibrator has no knots")
        s = np.asarray(scores, dtype=Float).ravel()
        # np.interp clamps to the endpoints outside the fitted range.
        return np.interp(s, self.x, self.y).astype(Float)

    def to_dict(self) -> dict[str, list[float]]:
        return {"x": [float(v) for v in self.x], "y": [float(v) for v in self.y]}

    @classmethod
    def from_dict(cls, d: dict[str, list[float]]) -> IsotonicCalibrator:
        return cls(x=np.array(d["x"], dtype=Float), y=np.array(d["y"], dtype=Float))


@dataclass(frozen=True)
class CalibrationDiagnostics:
    """Reliability of a set of probabilities against binary labels.

    ``ece`` is the expected calibration error (lower better); ``brier`` is the mean
    squared error of the probabilities (proper scoring rule); ``bin_confidence`` /
    ``bin_accuracy`` / ``bin_count`` are the reliability-diagram bins.
    """

    ece: float
    brier: float
    bin_confidence: tuple[float, ...]
    bin_accuracy: tuple[float, ...]
    bin_count: tuple[int, ...]


def reliability_curve(
    proba: Features, labels: Features, *, n_bins: int = 10
) -> CalibrationDiagnostics:
    """Compute ECE, Brier score, and the reliability-diagram bins."""

    p = np.asarray(proba, dtype=Float).ravel()
    y = np.asarray(labels, dtype=Float).ravel()
    if p.shape != y.shape:
        raise DataError("proba and labels must match")
    if p.size == 0:
        raise DataError("cannot diagnose empty input")
    yb = (y >= 0.5).astype(Float)
    ece = expected_calibration_error(p, yb, n_bins=n_bins)
    brier = float(np.mean((p - yb) ** 2))
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    conf: list[float] = []
    acc: list[float] = []
    cnt: list[int] = []
    for b in range(n_bins):
        lo, hi = edges[b], edges[b + 1]
        in_bin = (p > lo) & (p <= hi) if b > 0 else (p >= lo) & (p <= hi)
        n = int(in_bin.sum())
        cnt.append(n)
        if n == 0:
            conf.append(0.0)
            acc.append(0.0)
        else:
            conf.append(float(p[in_bin].mean()))
            acc.append(float(yb[in_bin].mean()))
    return CalibrationDiagnostics(
        ece=ece,
        brier=brier,
        bin_confidence=tuple(conf),
        bin_accuracy=tuple(acc),
        bin_count=tuple(cnt),
    )
