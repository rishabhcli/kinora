"""Pure paired statistics for the shadow-eval decision — no scipy, no RNG.

The promotion decision rests on a few classical results, all implemented here in
pure Python so the harness has no heavy numeric dependency and the tests pin exact
values:

* **Paired mean + Student-t CI** — for the per-shot quality delta. The paired
  design means we test whether the *mean of the differences* is above zero, with a
  two-sided ``(1-α)`` confidence interval. The t-quantile is obtained by inverting
  a numerically-stable regularised incomplete beta (no table lookups).
* **Win-rate + Wilson score interval** — the fraction of shots where the candidate
  strictly beat production, with an interval that behaves at the 0/1 extremes
  (unlike the naive normal interval).
* **Wilcoxon signed-rank** — a distribution-free companion to the t-test for the
  quality delta, robust to the non-normal score distributions metrics often have.
  Uses an exact enumeration for small n and a normal approximation (with tie +
  continuity correction) otherwise.
* **Deterministic bootstrap CI** — a percentile bootstrap whose resampling is
  driven by a *seeded* PRNG, so the interval is reproducible run-to-run.

Everything is a pure function of the input numbers; identical inputs ⇒ identical
outputs, which is what the unit tests assert.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConfidenceInterval:
    """A point estimate with a symmetric two-sided confidence interval."""

    estimate: float
    low: float
    high: float
    confidence: float

    @property
    def excludes_zero(self) -> bool:
        """True iff the whole interval is on one side of zero (a clear effect)."""
        return self.low > 0.0 or self.high < 0.0

    @property
    def is_positive(self) -> bool:
        """True iff the interval lies entirely above zero (candidate better)."""
        return self.low > 0.0


@dataclass(frozen=True, slots=True)
class PairedTTest:
    """Result of a one-sample (paired) t-test of the differences against zero."""

    n: int
    mean: float
    std: float
    t_statistic: float
    df: int
    p_value: float
    interval: ConfidenceInterval


@dataclass(frozen=True, slots=True)
class WinRate:
    """Fraction of paired shots the candidate strictly won, with a Wilson CI."""

    wins: int
    losses: int
    ties: int
    n: int
    rate: float
    interval: ConfidenceInterval


@dataclass(frozen=True, slots=True)
class SignedRankTest:
    """Wilcoxon signed-rank test of the differences against a zero median."""

    n: int
    statistic: float
    z: float
    p_value: float
    method: str  # "exact" | "normal"


# --------------------------------------------------------------------------- #
# Special functions (pure) — normal CDF, t CDF via incomplete beta.
# --------------------------------------------------------------------------- #


def normal_cdf(x: float) -> float:
    """Standard-normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _betacf(a: float, b: float, x: float) -> float:
    """Continued-fraction expansion for the incomplete beta (Lentz's method)."""
    max_iter = 200
    eps = 3.0e-12
    fpmin = 1.0e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def incomplete_beta(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta function ``I_x(a, b)`` in ``[0, 1]``."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(ln_beta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_cdf(t: float, df: int) -> float:
    """CDF of Student's t with ``df`` degrees of freedom."""
    if df <= 0:
        raise ValueError("df must be positive")
    x = df / (df + t * t)
    half = incomplete_beta(df / 2.0, 0.5, x) / 2.0
    return 1.0 - half if t > 0 else half


def student_t_quantile(p: float, df: int) -> float:
    """Inverse Student-t CDF (quantile) for ``p`` in ``(0, 1)`` via bisection."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    if df <= 0:
        raise ValueError("df must be positive")
    lo, hi = -1.0e6, 1.0e6
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if student_t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------- #
# Paired t-test on the differences.
# --------------------------------------------------------------------------- #


def _mean_std(values: Sequence[float]) -> tuple[float, float]:
    """Sample mean and *sample* (n-1) standard deviation."""
    n = len(values)
    mean = math.fsum(values) / n
    if n < 2:
        return mean, 0.0
    var = math.fsum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, math.sqrt(var)


def paired_t_test(differences: Sequence[float], *, confidence: float = 0.95) -> PairedTTest:
    """One-sample t-test of ``differences`` against a zero mean, with a CI.

    ``differences`` is the per-shot ``candidate - production`` vector. Returns the
    two-sided p-value and the ``confidence``-level interval for the mean delta.
    Requires ``n >= 2`` (a CI needs spread); raises otherwise.
    """
    n = len(differences)
    if n < 2:
        raise ValueError("paired t-test needs at least 2 differences")
    mean, std = _mean_std(differences)
    df = n - 1
    if std == 0.0:
        # Degenerate: all differences identical. The effect is exactly ``mean``
        # with no uncertainty; p is 0 if mean!=0 else 1.
        p = 0.0 if mean != 0.0 else 1.0
        interval = ConfidenceInterval(mean, mean, mean, confidence)
        inf_t = math.inf if mean > 0 else (-math.inf if mean < 0 else 0.0)
        return PairedTTest(n, mean, 0.0, inf_t, df, p, interval)
    se = std / math.sqrt(n)
    t_stat = mean / se
    p_value = 2.0 * (1.0 - student_t_cdf(abs(t_stat), df))
    t_crit = student_t_quantile(1.0 - (1.0 - confidence) / 2.0, df)
    margin = t_crit * se
    interval = ConfidenceInterval(mean, mean - margin, mean + margin, confidence)
    return PairedTTest(n, mean, std, t_stat, df, p_value, interval)


# --------------------------------------------------------------------------- #
# Win-rate with a Wilson score interval.
# --------------------------------------------------------------------------- #


def wilson_interval(successes: int, n: int, *, confidence: float = 0.95) -> ConfidenceInterval:
    """Wilson score interval for a binomial proportion ``successes / n``."""
    if n <= 0:
        return ConfidenceInterval(0.0, 0.0, 0.0, confidence)
    z = _z_for_confidence(confidence)
    phat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (phat + z2 / (2 * n)) / denom
    half = (z * math.sqrt(phat * (1.0 - phat) / n + z2 / (4 * n * n))) / denom
    return ConfidenceInterval(phat, max(0.0, center - half), min(1.0, center + half), confidence)


def _z_for_confidence(confidence: float) -> float:
    """Two-sided normal critical value for a ``confidence`` level."""
    p = 1.0 - (1.0 - confidence) / 2.0
    return _normal_quantile(p)


def _normal_quantile(p: float) -> float:
    """Inverse standard-normal CDF (Acklam's rational approximation)."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    ]
    plow = 0.02425
    phigh = 1.0 - plow
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
    )


def win_rate(
    differences: Sequence[float],
    *,
    margin: float = 0.0,
    confidence: float = 0.95,
) -> WinRate:
    """Candidate win-rate over paired quality deltas, with a Wilson CI.

    A *win* is a delta strictly greater than ``margin``; a *loss* is strictly less
    than ``-margin``; everything else is a tie (a dead-band that ignores
    practically-insignificant differences). The rate is ``wins / n`` over *all*
    pairs (ties count against the rate — a tie is not a win).
    """
    wins = sum(1 for d in differences if d > margin)
    losses = sum(1 for d in differences if d < -margin)
    n = len(differences)
    ties = n - wins - losses
    rate = wins / n if n else 0.0
    interval = wilson_interval(wins, n, confidence=confidence)
    return WinRate(wins=wins, losses=losses, ties=ties, n=n, rate=rate, interval=interval)


# --------------------------------------------------------------------------- #
# Wilcoxon signed-rank (distribution-free companion).
# --------------------------------------------------------------------------- #


def wilcoxon_signed_rank(differences: Sequence[float]) -> SignedRankTest:
    """Two-sided Wilcoxon signed-rank test of ``differences`` vs a zero median.

    Zero differences are dropped (standard Wilcoxon). Uses exact enumeration of the
    null distribution for ``n <= 18`` non-zero diffs, otherwise a normal
    approximation with tie + continuity correction.
    """
    nonzero = [d for d in differences if d != 0.0]
    n = len(nonzero)
    if n == 0:
        return SignedRankTest(n=0, statistic=0.0, z=0.0, p_value=1.0, method="exact")

    ranks = _average_ranks([abs(d) for d in nonzero])
    w_plus = math.fsum(r for d, r in zip(nonzero, ranks, strict=True) if d > 0)
    w_minus = math.fsum(r for d, r in zip(nonzero, ranks, strict=True) if d < 0)
    statistic = min(w_plus, w_minus)

    if n <= 18:
        p = _signed_rank_exact_p(statistic, n)
        mean = n * (n + 1) / 4.0
        se = math.sqrt(n * (n + 1) * (2 * n + 1) / 24.0)
        z = (statistic - mean) / se if se > 0 else 0.0
        return SignedRankTest(n=n, statistic=statistic, z=z, p_value=p, method="exact")

    mean = n * (n + 1) / 4.0
    tie_term = _tie_correction([abs(d) for d in nonzero])
    var = n * (n + 1) * (2 * n + 1) / 24.0 - tie_term / 48.0
    se = math.sqrt(var) if var > 0 else 0.0
    if se == 0.0:
        return SignedRankTest(n=n, statistic=statistic, z=0.0, p_value=1.0, method="normal")
    cc = 0.5  # continuity correction
    z = (statistic - mean + cc) / se
    p = 2.0 * normal_cdf(z)
    p = min(1.0, max(0.0, p))
    return SignedRankTest(n=n, statistic=statistic, z=z, p_value=p, method="normal")


def _average_ranks(values: Sequence[float]) -> list[float]:
    """Ranks of ``values`` (1-based), averaging ties."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j + 2) / 2.0  # mean of (i+1 .. j+1)
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _tie_correction(values: Sequence[float]) -> float:
    """``sum(t^3 - t)`` over tie groups, for the signed-rank variance."""
    counts: dict[float, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return float(sum(t**3 - t for t in counts.values() if t > 1))


def _signed_rank_exact_p(statistic: float, n: int) -> float:
    """Exact two-sided p-value for the signed-rank statistic (no ties assumed).

    Enumerates the null distribution of W+ by DP over rank sums; the two-sided p is
    twice the lower tail at ``statistic`` (capped at 1). Exact only without ties;
    with ties this is a close approximation (ties are rare in continuous deltas).
    """
    total = n * (n + 1) // 2
    # counts[s] = number of sign assignments giving W+ == s
    counts = [0] * (total + 1)
    counts[0] = 1
    for rank in range(1, n + 1):
        for s in range(total, rank - 1, -1):
            counts[s] += counts[s - rank]
    denom = float(1 << n)
    target = int(math.floor(statistic + 1e-9))
    lower = sum(counts[: target + 1]) / denom
    p = 2.0 * lower
    return min(1.0, p)


# --------------------------------------------------------------------------- #
# Deterministic percentile bootstrap CI.
# --------------------------------------------------------------------------- #


def bootstrap_mean_ci(
    values: Sequence[float],
    *,
    confidence: float = 0.95,
    iterations: int = 2000,
    seed: int = 0,
) -> ConfidenceInterval:
    """Percentile-bootstrap CI for the mean of ``values`` (seeded ⇒ reproducible).

    A non-parametric companion to the t-interval, useful when the delta
    distribution is skewed. The PRNG is seeded so identical inputs (and seed) give
    an identical interval — required for deterministic tests.
    """
    n = len(values)
    if n == 0:
        return ConfidenceInterval(0.0, 0.0, 0.0, confidence)
    point = math.fsum(values) / n
    if n == 1:
        return ConfidenceInterval(point, point, point, confidence)
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(iterations):
        resample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(math.fsum(resample) / n)
    means.sort()
    alpha = 1.0 - confidence
    lo = _percentile(means, alpha / 2.0)
    hi = _percentile(means, 1.0 - alpha / 2.0)
    return ConfidenceInterval(point, lo, hi, confidence)


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated ``q``-quantile of an already-sorted sequence."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_values[lo]
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


__all__ = [
    "ConfidenceInterval",
    "PairedTTest",
    "SignedRankTest",
    "WinRate",
    "bootstrap_mean_ci",
    "incomplete_beta",
    "normal_cdf",
    "paired_t_test",
    "student_t_cdf",
    "student_t_quantile",
    "wilcoxon_signed_rank",
    "wilson_interval",
    "win_rate",
]
