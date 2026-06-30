"""The statistics engine — compare a treatment arm to control, soundly.

This is the inferential core that decides whether a treatment video model is a
*winner* (significantly better on the primary metric) or has *breached* a
guardrail (significantly worse on a metric it must not regress). It is built on
the repo's proven, dependency-free :mod:`app.flags.stats` primitives so the math
is shared with the rest of the platform:

* a fixed-horizon **two-proportion z-test** and **Welch t-test** with Wald CIs,
  for a one-shot read at a pre-committed sample size;
* an **always-valid (mSPRT) sequential test** that you may peek at after every
  render without inflating the false-positive rate — the right tool for a live
  rollout that checks itself continuously.

What this module adds on top is *direction awareness*. A guardrail on
``cost_per_second`` or ``latency_ms`` is ``DECREASE`` (lower is better), so a
"regression" is the treatment going *up*; a guardrail on ``accept_rate`` is
``INCREASE``, so a regression is going *down*. Comparing a treatment to control
on a metric therefore can't just look at the raw difference — it has to know
which way good points. :func:`compare_proportion` / :func:`compare_mean` and
:func:`guardrail_breach` handle that in one place so callers never get the sign
wrong.

All functions are pure: they take stat summaries and a metric definition and
return a verdict. They never touch the collector, storage, a clock, or RNG.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.flags.stats import (
    AlwaysValidResult,
    ProportionStat,
    SampleStat,
    ZTestResult,
    msprt_proportion,
    relative_uplift,
    two_proportion_ztest,
    welch_ttest,
)
from app.video.experiments.models import MetricDirection, VideoMetric

# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Comparison:
    """A direction-aware verdict comparing one treatment arm to control.

    Attributes:
        metric_key: Which metric this compares.
        control_value: Observed control rate (proportion) or mean (continuous).
        treatment_value: Observed treatment rate/mean.
        absolute_diff: ``treatment − control`` in raw units (sign is *raw*, not
            direction-adjusted — see ``treatment_better``).
        relative_change: ``(treatment − control) / control`` (0 when control 0).
        p_value: Always-valid p-value from the sequential test (safe to peek).
        ci_low / ci_high: Confidence sequence on the absolute difference.
        significant: ``p_value < alpha`` — the difference is real.
        treatment_better: True when the treatment moved in the metric's *good*
            direction (accounts for INCREASE vs DECREASE).
        samples: Total samples across both arms backing this comparison.
    """

    metric_key: str
    control_value: float
    treatment_value: float
    absolute_diff: float
    relative_change: float
    p_value: float
    ci_low: float
    ci_high: float
    significant: bool
    treatment_better: bool
    samples: int

    @property
    def significant_win(self) -> bool:
        """A real improvement in the good direction (the bar for promotion)."""
        return self.significant and self.treatment_better

    @property
    def significant_regression(self) -> bool:
        """A real move in the *bad* direction (the bar for guardrail rollback)."""
        return self.significant and not self.treatment_better

    def to_dict(self) -> dict[str, object]:
        return {
            "metric_key": self.metric_key,
            "control_value": self.control_value,
            "treatment_value": self.treatment_value,
            "absolute_diff": self.absolute_diff,
            "relative_change": self.relative_change,
            "p_value": self.p_value,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "significant": self.significant,
            "treatment_better": self.treatment_better,
            "samples": self.samples,
        }


def _better(direction: MetricDirection, diff: float) -> bool:
    """Does a raw ``treatment − control`` diff point the *good* way?"""
    if diff == 0.0:
        return False
    return diff > 0.0 if direction is MetricDirection.INCREASE else diff < 0.0


def compare_proportion(
    metric: VideoMetric,
    control: ProportionStat,
    treatment: ProportionStat,
    *,
    alpha: float = 0.05,
    sequential: bool = True,
) -> Comparison:
    """Compare two arms on a proportion metric (always-valid by default)."""
    if sequential:
        seq: AlwaysValidResult = msprt_proportion(control, treatment, alpha=alpha)
        diff = seq.estimate
        return Comparison(
            metric_key=metric.key,
            control_value=control.rate,
            treatment_value=treatment.rate,
            absolute_diff=diff,
            relative_change=relative_uplift(control.rate, treatment.rate),
            p_value=seq.p_value,
            ci_low=seq.ci_low,
            ci_high=seq.ci_high,
            significant=seq.decisive,
            treatment_better=_better(metric.direction, diff),
            samples=seq.samples,
        )
    fixed: ZTestResult = two_proportion_ztest(control, treatment, alpha=alpha)
    return Comparison(
        metric_key=metric.key,
        control_value=control.rate,
        treatment_value=treatment.rate,
        absolute_diff=fixed.estimate,
        relative_change=relative_uplift(control.rate, treatment.rate),
        p_value=fixed.p_value,
        ci_low=fixed.ci_low,
        ci_high=fixed.ci_high,
        significant=fixed.significant,
        treatment_better=_better(metric.direction, fixed.estimate),
        samples=control.trials + treatment.trials,
    )


def compare_mean(
    metric: VideoMetric,
    control: SampleStat,
    treatment: SampleStat,
    *,
    alpha: float = 0.05,
) -> Comparison:
    """Compare two arms on a continuous metric via Welch's t-test.

    The continuous test is fixed-horizon (Welch); pair it with the experiment's
    ``min_samples_per_arm`` floor so you only read it once enough data is in.
    """
    res: ZTestResult = welch_ttest(control, treatment, alpha=alpha)
    rel = 0.0 if control.mean == 0.0 else (treatment.mean - control.mean) / control.mean
    return Comparison(
        metric_key=metric.key,
        control_value=control.mean,
        treatment_value=treatment.mean,
        absolute_diff=res.estimate,
        relative_change=rel,
        p_value=res.p_value,
        ci_low=res.ci_low,
        ci_high=res.ci_high,
        significant=res.significant,
        treatment_better=_better(metric.direction, res.estimate),
        samples=control.count + treatment.count,
    )


# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GuardrailVerdict:
    """Whether a treatment arm has breached a guardrail metric.

    Attributes:
        metric_key: The guardrail metric.
        breached: True when the treatment has *significantly* regressed past the
            tolerated margin — the signal to halt/rollback the arm.
        comparison: The underlying direction-aware comparison.
        detail: A human one-liner for the report/log.
    """

    metric_key: str
    breached: bool
    comparison: Comparison
    detail: str


def guardrail_breach(
    metric: VideoMetric,
    comparison: Comparison,
    *,
    min_samples: int = 1,
) -> GuardrailVerdict:
    """Decide whether ``comparison`` constitutes a breach of ``metric``.

    A breach requires *all* of:

    #. enough data (``samples >= min_samples``) — never rollback on noise;
    #. a statistically significant difference (the confidence sequence excludes
       no-effect at the test's α);
    #. the difference is in the *bad* direction; and
    #. the magnitude exceeds the metric's tolerated relative margin, i.e. the
       treatment didn't just slip by a hair but past the agreed threshold.

    The margin is applied to the control value: tolerating a 5% relative
    regression on a control accept-rate of 0.80 means a breach only fires once
    the treatment is significantly below ``0.80 * (1 − 0.05) = 0.76``.
    """
    if not metric.is_guardrail:
        return GuardrailVerdict(metric.key, False, comparison, "not a guardrail metric")
    if comparison.samples < min_samples:
        return GuardrailVerdict(
            metric.key, False, comparison, f"insufficient samples ({comparison.samples})"
        )
    if not comparison.significant_regression:
        return GuardrailVerdict(metric.key, False, comparison, "no significant regression")

    # Magnitude check against the tolerated relative margin.
    margin = abs(metric.guardrail_margin)
    tolerated_rel = -margin if metric.direction is MetricDirection.INCREASE else margin

    # Degenerate baseline: when control sits at ~0, a *relative* margin is
    # undefined (any positive treatment value is an infinite relative move), so
    # ``relative_change`` collapses to 0 and would spuriously read as "within
    # margin". A significant regression against a zero baseline is always a real
    # breach — the absolute confidence sequence already proved the effect — so
    # fall back to the significance signal rather than the relative gate.
    if comparison.control_value == 0.0:
        return GuardrailVerdict(
            metric.key,
            True,
            comparison,
            f"guardrail breached: significant regression against ~zero baseline "
            f"(treatment {comparison.treatment_value:.4f})",
        )

    # relative_change is signed in raw units; convert "bad direction" into a
    # single positive "how far past tolerance" number.
    if metric.direction is MetricDirection.INCREASE:
        # bad = dropped; breach if relative drop exceeds the margin.
        over = (-comparison.relative_change) > margin
    else:
        # bad = rose; breach if relative rise exceeds the margin.
        over = comparison.relative_change > margin
    if not over:
        return GuardrailVerdict(
            metric.key,
            False,
            comparison,
            f"regression within tolerated margin {margin:.3f} "
            f"(rel change {comparison.relative_change:+.3f})",
        )
    return GuardrailVerdict(
        metric.key,
        True,
        comparison,
        f"guardrail breached: {comparison.relative_change:+.3f} relative "
        f"(tolerated {tolerated_rel:+.3f})",
    )


__all__ = [
    "Comparison",
    "GuardrailVerdict",
    "compare_mean",
    "compare_proportion",
    "guardrail_breach",
]
