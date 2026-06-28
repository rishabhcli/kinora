"""Significance math for experiments — fixed-horizon *and* sequential-safe.

A/B "peeking" (checking significance after every event and stopping the moment
p < 0.05) inflates the false-positive rate far above the nominal α, because each
peek is another chance to cross the line by noise. The honest answer is an
**always-valid** test whose error guarantee holds *no matter how often you look*.

This module provides both:

* :func:`two_proportion_ztest` / :func:`welch_ttest` — classic fixed-horizon
  tests (valid only at a single pre-committed sample size). Useful when N is
  fixed in advance (e.g. the §13 harness runs exactly N shots per arm).
* :func:`msprt_proportion` — a **mixture Sequential Probability Ratio Test** for
  a difference in two Bernoulli rates. It yields an always-valid p-value and a
  confidence sequence (a CI you may inspect continuously); type-I error stays
  ≤ α under unlimited peeking. This is what a live product rollout should use.
* :func:`guardrail_breached` — a one-sided always-valid check that the treatment
  has not *regressed* a guardrail metric beyond a tolerated margin.

Everything is dependency-free (stdlib ``math`` only) and pure.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.flags.errors import StatsError

# --------------------------------------------------------------------------- #
# Normal distribution helpers (stdlib only; no scipy dependency)
# --------------------------------------------------------------------------- #


def _norm_cdf(z: float) -> float:
    """Standard-normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _norm_sf(z: float) -> float:
    """Standard-normal survival function ``P(Z > z)``."""
    return 1.0 - _norm_cdf(z)


def two_sided_p(z: float) -> float:
    """Two-sided p-value for a z statistic."""
    return 2.0 * _norm_sf(abs(z))


# --------------------------------------------------------------------------- #
# Summary stats containers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProportionStat:
    """A binomial summary: ``successes`` out of ``trials`` (a conversion rate)."""

    successes: int
    trials: int

    def __post_init__(self) -> None:
        if self.trials < 0 or self.successes < 0:
            raise StatsError("proportion counts must be non-negative")
        if self.successes > self.trials:
            raise StatsError("successes cannot exceed trials")

    @property
    def rate(self) -> float:
        """Observed rate (0 when no trials)."""
        return self.successes / self.trials if self.trials else 0.0


@dataclass(frozen=True, slots=True)
class SampleStat:
    """A continuous-metric summary: count, mean, and (sample) variance."""

    count: int
    mean: float
    variance: float

    def __post_init__(self) -> None:
        if self.count < 0:
            raise StatsError("sample count must be non-negative")
        if self.variance < 0:
            raise StatsError("variance must be non-negative")

    @classmethod
    def from_values(cls, values: list[float]) -> SampleStat:
        """Build a summary from raw values (sample variance, ddof=1)."""
        n = len(values)
        if n == 0:
            return cls(0, 0.0, 0.0)
        mean = sum(values) / n
        if n == 1:
            return cls(1, mean, 0.0)
        var = sum((v - mean) ** 2 for v in values) / (n - 1)
        return cls(n, mean, var)


@dataclass(frozen=True, slots=True)
class ZTestResult:
    """Fixed-horizon z/t test outcome."""

    estimate: float  # treatment − control (difference of means/rates)
    z: float
    p_value: float
    ci_low: float
    ci_high: float
    significant: bool


@dataclass(frozen=True, slots=True)
class AlwaysValidResult:
    """An always-valid (sequential) test outcome — safe to inspect at any N.

    ``p_value`` is an anytime-valid p-value (1 / likelihood-ratio, clamped to 1);
    ``ci_low``/``ci_high`` form a confidence *sequence* whose simultaneous
    coverage is ``1 − alpha`` across all sample sizes. ``decisive`` is True once
    the p-value crosses ``alpha`` — at which point you may stop.
    """

    estimate: float
    log_likelihood_ratio: float
    p_value: float
    ci_low: float
    ci_high: float
    decisive: bool
    samples: int


# --------------------------------------------------------------------------- #
# Fixed-horizon tests
# --------------------------------------------------------------------------- #


def two_proportion_ztest(
    control: ProportionStat, treatment: ProportionStat, *, alpha: float = 0.05
) -> ZTestResult:
    """Two-sided pooled-variance z-test for a difference in two rates.

    Valid only at a single pre-committed sample size. The CI is the
    *unpooled* Wald interval on the rate difference at level ``1 − alpha``.
    """
    if not 0.0 < alpha < 1.0:
        raise StatsError("alpha must be in (0, 1)")
    if control.trials == 0 or treatment.trials == 0:
        return ZTestResult(0.0, 0.0, 1.0, 0.0, 0.0, False)

    p1, p2 = control.rate, treatment.rate
    n1, n2 = control.trials, treatment.trials
    estimate = p2 - p1

    pooled = (control.successes + treatment.successes) / (n1 + n2)
    se_pooled = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    z = 0.0 if se_pooled == 0 else estimate / se_pooled
    p_value = two_sided_p(z)

    se_unpooled = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    half = _z_crit(alpha) * se_unpooled
    return ZTestResult(estimate, z, p_value, estimate - half, estimate + half, p_value < alpha)


def welch_ttest(
    control: SampleStat, treatment: SampleStat, *, alpha: float = 0.05
) -> ZTestResult:
    """Welch's unequal-variance t-test for a difference in two means.

    For experiment-scale N the t distribution is ~normal, so we report the
    normal-approximation p-value/CI (no special-function inverse-t needed) which
    is accurate to well within rounding at the sample sizes experiments hit.
    """
    if not 0.0 < alpha < 1.0:
        raise StatsError("alpha must be in (0, 1)")
    if control.count < 2 or treatment.count < 2:
        return ZTestResult(treatment.mean - control.mean, 0.0, 1.0, 0.0, 0.0, False)

    se = math.sqrt(control.variance / control.count + treatment.variance / treatment.count)
    estimate = treatment.mean - control.mean
    z = 0.0 if se == 0 else estimate / se
    p_value = two_sided_p(z)
    half = _z_crit(alpha) * se
    return ZTestResult(estimate, z, p_value, estimate - half, estimate + half, p_value < alpha)


def _z_crit(alpha: float) -> float:
    """Two-sided critical z for ``alpha`` (inverse normal via bisection)."""
    target = 1.0 - alpha / 2.0
    lo, hi = 0.0, 8.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if _norm_cdf(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


# --------------------------------------------------------------------------- #
# Always-valid (sequential) test — mSPRT for two proportions
# --------------------------------------------------------------------------- #


def msprt_proportion(
    control: ProportionStat,
    treatment: ProportionStat,
    *,
    alpha: float = 0.05,
    tau: float = 0.05,
) -> AlwaysValidResult:
    """Mixture-SPRT always-valid test for a difference in two Bernoulli rates.

    Tests H0: ``p_treatment == p_control`` against a two-sided alternative,
    mixing the alternative over a zero-mean normal prior on the standardized
    effect with variance ``tau`` (the "mixing variance" — larger ``tau`` favors
    detecting bigger effects sooner). The returned ``p_value`` is anytime-valid:
    ``P(ever reject | H0) ≤ alpha`` for *any* stopping rule, so you may peek
    after every event and stop the instant ``decisive`` is True.

    The confidence sequence is a normal-mixture interval on the rate difference
    that is wider than a fixed-horizon CI (the price of unlimited peeking) and
    tightens as evidence accumulates.

    Derivation: with pooled rate ``p̂`` the difference estimator ``Δ̂`` has
    information ``V = 1 / (p̂(1−p̂)(1/n1+1/n2))``. The mixture likelihood ratio is
        Λ = sqrt(V / (V + 1/τ)) · exp( 0.5 · Δ̂² · V² / (V + 1/τ) ),
    and the anytime p-value is ``min(1, 1/Λ)``.
    """
    if not 0.0 < alpha < 1.0:
        raise StatsError("alpha must be in (0, 1)")
    if tau <= 0.0:
        raise StatsError("tau (mixing variance) must be positive")

    n1, n2 = control.trials, treatment.trials
    samples = n1 + n2
    if n1 == 0 or n2 == 0:
        return AlwaysValidResult(0.0, 0.0, 1.0, float("-inf"), float("inf"), False, samples)

    p1, p2 = control.rate, treatment.rate
    estimate = p2 - p1
    pooled = (control.successes + treatment.successes) / samples
    # Degenerate pooled variance (all-zero or all-one): no usable signal.
    var_unit = pooled * (1.0 - pooled)
    if var_unit <= 0.0:
        return AlwaysValidResult(estimate, 0.0, 1.0, estimate, estimate, False, samples)

    se2 = var_unit * (1.0 / n1 + 1.0 / n2)  # Var(Δ̂)
    info = 1.0 / se2  # Fisher information V

    denom = info + 1.0 / tau
    log_lr = 0.5 * math.log(info / denom) + 0.5 * (estimate**2) * (info**2) / denom
    p_value = min(1.0, math.exp(-log_lr))

    # Confidence sequence on Δ: { d : mixture LR for offset d ≤ 1/alpha }.
    # Solving the quadratic in (Δ̂ − d) gives a symmetric interval of half-width:
    radius_term = 2.0 * math.log(1.0 / alpha) + math.log(denom / info)
    half = math.sqrt(max(0.0, radius_term * denom / (info**2)))
    return AlwaysValidResult(
        estimate=estimate,
        log_likelihood_ratio=log_lr,
        p_value=p_value,
        ci_low=estimate - half,
        ci_high=estimate + half,
        decisive=p_value < alpha,
        samples=samples,
    )


def guardrail_breached(
    control: ProportionStat,
    treatment: ProportionStat,
    *,
    max_relative_regression: float = 0.0,
    alpha: float = 0.01,
) -> bool:
    """Always-valid one-sided guardrail: has treatment *regressed* control?

    Returns True when the treatment rate is significantly *below* control's rate
    by more than ``max_relative_regression`` (a fraction, e.g. ``0.02`` tolerates
    a 2% relative dip) under an always-valid test at level ``alpha``. Use for a
    metric where lower is worse (retention, completion) — wire the *inverse* for
    "lower is better" metrics. A breach is a signal to halt the rollout.
    """
    result = msprt_proportion(control, treatment, alpha=alpha)
    tolerated = -abs(max_relative_regression) * control.rate
    # Breach: the whole confidence sequence sits below the tolerated floor.
    return result.ci_high < tolerated


def relative_uplift(control_rate: float, treatment_rate: float) -> float:
    """Relative change ``(treatment − control) / control`` (0 when control is 0)."""
    if control_rate == 0.0:
        return 0.0
    return (treatment_rate - control_rate) / control_rate


def required_sample_size(
    baseline_rate: float,
    *,
    mde: float,
    alpha: float = 0.05,
    power: float = 0.8,
) -> int:
    """Per-arm sample size to detect a relative ``mde`` at ``alpha``/``power``.

    Standard two-proportion power formula. ``mde`` is the minimum *relative*
    detectable effect (e.g. ``0.1`` for a 10% lift). Returns a per-arm count;
    multiply by the number of arms for the total.
    """
    if not 0.0 < baseline_rate < 1.0:
        raise StatsError("baseline_rate must be in (0, 1)")
    if mde <= 0.0:
        raise StatsError("mde must be positive")
    p1 = baseline_rate
    p2 = baseline_rate * (1.0 + mde)
    p2 = min(p2, 0.999999)
    z_a = _z_crit(alpha)
    z_b = _inv_norm(power)
    pbar = (p1 + p2) / 2.0
    numerator = (
        z_a * math.sqrt(2 * pbar * (1 - pbar)) + z_b * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))
    ) ** 2
    return math.ceil(numerator / ((p2 - p1) ** 2))


def _inv_norm(p: float) -> float:
    """Inverse standard-normal CDF via bisection (for power calcs)."""
    if not 0.0 < p < 1.0:
        raise StatsError("probability must be in (0, 1)")
    lo, hi = -8.0, 8.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if _norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


__all__ = [
    "AlwaysValidResult",
    "ProportionStat",
    "SampleStat",
    "ZTestResult",
    "guardrail_breached",
    "msprt_proportion",
    "relative_uplift",
    "required_sample_size",
    "two_proportion_ztest",
    "two_sided_p",
    "welch_ttest",
]
