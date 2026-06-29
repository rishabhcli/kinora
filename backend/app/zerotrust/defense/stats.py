"""Lightweight unsupervised-statistics primitives for streaming detection.

These are the *models* the threat detectors compose. Every one is:

* **online** — it updates from one observation at a time in O(1) (no buffering of
  the whole stream), which is what a streaming engine needs; and
* **deterministic** — given the same observations (and the same seed for the
  isolation forest) it yields identical state, so synthetic-trace tests are
  byte-stable.

Provided here:

* :class:`Ewma` — exponentially-weighted moving average + variance (a smooth
  baseline whose half-life is expressed in *time*, decay-correct under bursts);
* :class:`Mad` — a rolling median / median-absolute-deviation robust z-score
  (resistant to the outliers it is meant to catch, unlike a mean/stddev z);
* :class:`RobustScaler` — combines an EWMA centre with a MAD scale into a single
  bounded anomaly score in ``0..1``;
* :class:`IsolationForestLite` — a tiny, fixed-size isolation forest for
  low-dimensional behavioural feature vectors. Deliberately pure-Python (the
  feature space is small and the forest stays in memory), so the package imports
  and runs anywhere without numpy and stays byte-deterministic given a seed.

No module-scope randomness, no global import side effects.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field


def logistic(x: float, *, k: float = 1.0) -> float:
    """Numerically-stable logistic squashing ``(-inf,inf) -> (0,1)``."""
    if x >= 0:
        z = math.exp(-k * x)
        return 1.0 / (1.0 + z)
    z = math.exp(k * x)
    return z / (1.0 + z)


def clamp01(x: float) -> float:
    """Clamp into the closed unit interval."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


@dataclass(slots=True)
class Ewma:
    """Time-decayed exponentially-weighted mean and variance.

    The decay is expressed as a **half-life in seconds**: an observation's weight
    halves every ``half_life`` seconds of elapsed time, so the baseline adapts at
    a rate you can reason about regardless of event cadence. Variance is tracked
    with West's incremental form adapted to weighting, giving a running stddev
    for z-scoring.
    """

    half_life: float
    mean: float = 0.0
    var: float = 0.0
    _weight: float = 0.0
    _last_t: float | None = None
    initialized: bool = False

    def __post_init__(self) -> None:
        if self.half_life <= 0:
            raise ValueError("half_life must be positive")

    @property
    def _decay_lambda(self) -> float:
        return math.log(2.0) / self.half_life

    def observe(self, value: float, t: float) -> None:
        """Fold in ``value`` observed at monotonic time ``t``."""
        if not self.initialized:
            self.mean = value
            self.var = 0.0
            self._weight = 1.0
            self._last_t = t
            self.initialized = True
            return
        dt = max(0.0, t - (self._last_t if self._last_t is not None else t))
        decay = math.exp(-self._decay_lambda * dt)
        self._weight = self._weight * decay + 1.0
        self._last_t = t
        alpha = 1.0 / self._weight
        delta = value - self.mean
        self.mean += alpha * delta
        # Exponentially-weighted variance (Welford-style with the new alpha).
        self.var = (1.0 - alpha) * (self.var + alpha * delta * delta)

    @property
    def stddev(self) -> float:
        return math.sqrt(max(0.0, self.var))

    def zscore(self, value: float) -> float:
        """Standard score of ``value`` against the current baseline."""
        sd = self.stddev
        if sd <= 1e-9:
            return 0.0
        return (value - self.mean) / sd


@dataclass(slots=True)
class Mad:
    """Rolling median + median-absolute-deviation robust z-score.

    Keeps a bounded window of the most recent ``window`` observations and derives
    the median and MAD from it. The robust z-score uses the 1.4826 consistency
    constant so that, for normally-distributed data, the MAD scale matches the
    standard deviation — making thresholds interpretable in "sigma" terms while
    staying immune to the very spikes a detector hunts for.
    """

    window: int = 256
    _buf: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.window < 4:
            raise ValueError("MAD window must be >= 4")

    def observe(self, value: float) -> None:
        self._buf.append(value)
        if len(self._buf) > self.window:
            del self._buf[0]

    @property
    def ready(self) -> bool:
        return len(self._buf) >= 4

    @staticmethod
    def _median(values: Sequence[float]) -> float:
        s = sorted(values)
        n = len(s)
        if n == 0:
            return 0.0
        mid = n // 2
        if n % 2:
            return s[mid]
        return 0.5 * (s[mid - 1] + s[mid])

    @property
    def median(self) -> float:
        return self._median(self._buf)

    @property
    def mad(self) -> float:
        med = self.median
        return self._median([abs(v - med) for v in self._buf])

    def robust_z(self, value: float) -> float:
        """Robust standard score; 0 when the scale is degenerate."""
        if not self.ready:
            return 0.0
        scale = 1.4826 * self.mad
        if scale <= 1e-9:
            # Degenerate spread: flag only a strict departure from the median.
            return 0.0 if value == self.median else math.copysign(6.0, value - self.median)
        return (value - self.median) / scale


@dataclass(slots=True)
class RobustScaler:
    """Compose an EWMA centre + MAD scale into a bounded ``0..1`` anomaly score.

    Only *positive* departures (values above the baseline) count as anomalous for
    rate-style signals — a quiet period is never an attack. Set
    ``two_sided=True`` for signals where unusually-low is also suspicious.
    """

    half_life: float = 60.0
    window: int = 256
    two_sided: bool = False
    sensitivity: float = 0.6
    ewma: Ewma = field(init=False)
    mad: Mad = field(init=False)

    def __post_init__(self) -> None:
        self.ewma = Ewma(half_life=self.half_life)
        self.mad = Mad(window=self.window)

    def update(self, value: float, t: float) -> None:
        self.ewma.observe(value, t)
        self.mad.observe(value)

    def score(self, value: float) -> float:
        z_robust = self.mad.robust_z(value)
        z_ewma = self.ewma.zscore(value)
        # Take the more conservative (smaller magnitude) of the two so a cold
        # MAD window can't fire on its own before it has enough data.
        if self.mad.ready:
            z = z_robust if abs(z_robust) <= abs(z_ewma) or z_ewma == 0 else z_ewma
        else:
            z = z_ewma
        if not self.two_sided:
            z = max(0.0, z)
        return clamp01(logistic(abs(z) - 3.0, k=self.sensitivity) * 1.0)

    def observe_and_score(self, value: float, t: float) -> float:
        """Score ``value`` against the current baseline *then* fold it in."""
        s = self.score(value)
        self.update(value, t)
        return s


# --------------------------------------------------------------------------- #
# Isolation-forest-lite
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _INode:
    """A node in an isolation tree (internal: split, or leaf: size)."""

    feature: int = -1
    threshold: float = 0.0
    left: _INode | None = None
    right: _INode | None = None
    size: int = 0
    depth: int = 0


def _c(n: int) -> float:
    """Average path length of an unsuccessful BST search over ``n`` points.

    The isolation-forest normalisation constant (Liu et al. 2008).
    """
    if n <= 1:
        return 0.0
    h = math.log(n - 1) + 0.5772156649015329  # Euler-Mascheroni
    return 2.0 * h - 2.0 * (n - 1) / n


class IsolationForestLite:
    """A compact isolation forest for low-dimensional behavioural vectors.

    Trees are built on a subsample, splits are random feature/threshold pairs,
    and a point's anomaly score is the standard ``2 ** (-E[h]/c(psi))`` so a
    score near ``1`` is a strong outlier and ``~0.5`` is in-distribution. Seeded
    RNG ⇒ fully deterministic, pure-Python (the feature space is small).

    Features are **per-dimension z-standardised** against the training set before
    trees are built (and query points are standardised with the same statistics).
    Without this a raw feature like "41 distinct paths" dwarfs a ratio in ``[0,1]``
    and the high-magnitude dimensions monopolise every random split — the model
    would then be blind to anomalies that live in the small-scale dimensions.
    """

    def __init__(
        self,
        *,
        n_trees: int = 64,
        sample_size: int = 256,
        max_depth: int | None = None,
        seed: int = 0,
    ) -> None:
        if n_trees < 1:
            raise ValueError("n_trees must be >= 1")
        if sample_size < 4:
            raise ValueError("sample_size must be >= 4")
        self.n_trees = n_trees
        self.sample_size = sample_size
        self.max_depth = (
            max_depth if max_depth is not None else int(math.ceil(math.log2(sample_size)))
        )
        self.seed = seed
        self._trees: list[_INode] = []
        self._psi: int = 0
        self._fitted = False
        self._mean: list[float] = []
        self._std: list[float] = []
        self._lo: list[float] = []
        self._hi: list[float] = []

    @property
    def fitted(self) -> bool:
        return self._fitted

    def _standardize(self, row: Sequence[float]) -> list[float]:
        return [(float(x) - m) / s for x, m, s in zip(row, self._mean, self._std, strict=False)]

    def fit(self, data: Sequence[Sequence[float]]) -> IsolationForestLite:
        raw = [list(map(float, r)) for r in data]
        if not raw:
            raise ValueError("cannot fit on empty data")
        n = len(raw)
        dim = len(raw[0])
        if any(len(r) != dim for r in raw):
            raise ValueError("all feature vectors must share a dimension")
        # Per-dimension standardisation statistics (population stddev, floored so
        # a zero-variance dimension maps to 0 and never divides by zero).
        self._mean = [sum(r[j] for r in raw) / n for j in range(dim)]
        self._std = []
        self._lo = [min(r[j] for r in raw) for j in range(dim)]
        self._hi = [max(r[j] for r in raw) for j in range(dim)]
        for j in range(dim):
            var = sum((r[j] - self._mean[j]) ** 2 for r in raw) / n
            self._std.append(max(1e-9, math.sqrt(var)))
        rows = [self._standardize(r) for r in raw]
        self._psi = min(self.sample_size, n)
        self._trees = []
        for ti in range(self.n_trees):
            rng = random.Random((self.seed << 16) ^ (ti * 2654435761 & 0xFFFFFFFF))
            sample = rng.sample(rows, self._psi) if self._psi < n else list(rows)
            self._trees.append(self._build(sample, 0, rng))
        self._fitted = True
        return self

    def _build(self, rows: list[list[float]], depth: int, rng: random.Random) -> _INode:
        n = len(rows)
        if depth >= self.max_depth or n <= 1:
            return _INode(size=n, depth=depth)
        dim = len(rows[0])
        feature = rng.randrange(dim)
        col = [r[feature] for r in rows]
        lo, hi = min(col), max(col)
        if lo == hi:
            return _INode(size=n, depth=depth)
        threshold = rng.uniform(lo, hi)
        left = [r for r in rows if r[feature] < threshold]
        right = [r for r in rows if r[feature] >= threshold]
        if not left or not right:
            return _INode(size=n, depth=depth)
        return _INode(
            feature=feature,
            threshold=threshold,
            depth=depth,
            left=self._build(left, depth + 1, rng),
            right=self._build(right, depth + 1, rng),
        )

    @staticmethod
    def _path_length(node: _INode, point: Sequence[float]) -> float:
        depth = 0.0
        cur: _INode | None = node
        while cur is not None and cur.feature >= 0:
            depth += 1.0
            cur = cur.left if point[cur.feature] < cur.threshold else cur.right
        if cur is not None:
            depth += _c(cur.size)
        return depth

    def score(self, point: Sequence[float]) -> float:
        """Anomaly score in ``0..1``; higher = more isolated/outlying."""
        if not self._fitted:
            raise RuntimeError("IsolationForestLite.score called before fit()")
        pt = self._standardize(point)
        avg = sum(self._path_length(t, pt) for t in self._trees) / len(self._trees)
        denom = _c(self._psi)
        iso = 0.0 if denom <= 0 else 2.0 ** (-avg / denom)
        # Isolation trees are structurally blind to a feature that is constant in
        # the training set, yet a query far outside that feature's observed range
        # is exactly an anomaly. Blend an explicit out-of-support (novelty) term so
        # the model is not fooled by low-variance dimensions; the final score is
        # the stronger of "hard to isolate" and "outside known support".
        return max(iso, self.novelty(point))

    def novelty(self, point: Sequence[float]) -> float:
        """``0..1`` out-of-support score: how far ``point`` lies outside the
        per-dimension ``[min, max]`` range observed during ``fit``.

        For each dimension the excursion beyond the training range is measured in
        units of that dimension's spread (its range, or its mean magnitude when
        the dimension was constant), squashed through a logistic. The dimension
        with the largest excursion drives the score.
        """
        if not self._fitted:
            raise RuntimeError("IsolationForestLite.novelty called before fit()")
        worst = 0.0
        for x, lo, hi in zip(point, self._lo, self._hi, strict=False):
            xf = float(x)
            if lo <= xf <= hi:
                continue
            span = hi - lo
            if span <= 1e-12:
                # Constant training dimension: scale by the value's own magnitude
                # (floored at 1.0) so a *gross* departure (e.g. 41 vs a constant 1)
                # is large while a tiny ratio departure (0.05 vs a constant 0) is
                # correctly negligible — boundary-grazing must not look anomalous.
                scale = max(1.0, abs(lo), abs(hi))
            else:
                scale = span
            excursion = (lo - xf if xf < lo else xf - hi) / scale
            worst = max(worst, excursion)
        if worst <= 0.0:
            return 0.0
        return clamp01(logistic(worst - 1.0, k=1.5))


__all__ = [
    "Ewma",
    "IsolationForestLite",
    "Mad",
    "RobustScaler",
    "clamp01",
    "logistic",
]
