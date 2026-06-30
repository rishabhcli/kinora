"""Promotion recommendation — turn a paired analysis into a go/no-go for a canary.

Shadow eval never flips a model into reader traffic on its own; it produces a
*recommendation* that a human (or the rollout controller) acts on. This module
applies a transparent set of :class:`PromotionThresholds` to a
:class:`ComparisonAnalysis` and returns a :class:`PromotionRecommendation` whose
verdict is one of:

* ``PROMOTE``    — clear evidence the candidate is at least as good and not more
  costly/fragile: safe to start a real canary.
* ``HOLD``       — not enough evidence yet (too few comparable samples, or the
  quality CI straddles the no-worse line): keep collecting.
* ``REJECT``     — clear evidence the candidate is worse on quality, or materially
  more costly / less reliable: do not canary.

Every gate records a typed :class:`ReasonCode` so the report explains *why*. The
decision is a pure function of the analysis + thresholds — deterministic and
unit-tested against hand-built analyses.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .analysis import ComparisonAnalysis


class Verdict(StrEnum):
    """The recommendation outcome."""

    PROMOTE = "promote"
    HOLD = "hold"
    REJECT = "reject"


class ReasonCode(StrEnum):
    """A typed, explainable reason that drove (or blocked) the verdict."""

    INSUFFICIENT_SAMPLES = "insufficient_samples"
    NO_COMPARABLE_PAIRS = "no_comparable_pairs"
    QUALITY_REGRESSION = "quality_regression"
    QUALITY_INCONCLUSIVE = "quality_inconclusive"
    QUALITY_NON_INFERIOR = "quality_non_inferior"
    QUALITY_IMPROVEMENT = "quality_improvement"
    WIN_RATE_BELOW_FLOOR = "win_rate_below_floor"
    COST_REGRESSION = "cost_regression"
    RELIABILITY_REGRESSION = "reliability_regression"
    ALL_GATES_PASSED = "all_gates_passed"


class PromotionThresholds(BaseModel):
    """The (operator-tunable) bar a candidate must clear for a canary.

    Defaults are conservative: a candidate may be promoted only with enough paired
    evidence, a non-inferior quality CI, an acceptable win-rate, and no material
    cost or reliability regression.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Minimum comparable (both-succeeded, both-scored) paired samples before *any*
    #: non-HOLD verdict is allowed.
    min_comparable_samples: int = 30
    #: A mean quality delta whose two-sided CI low is below this is a regression.
    #: ``0.0`` ⇒ require the CI to not dip below parity (strict non-inferiority).
    min_quality_ci_low: float = 0.0
    #: Win-rate (after the dead-band) the candidate must reach to PROMOTE.
    min_win_rate: float = 0.5
    #: Largest per-shot *increase* in video-seconds tolerated (cost regression
    #: above this ⇒ REJECT). ``0.0`` ⇒ no cost increase allowed.
    max_cost_increase_s: float = 0.0
    #: Largest increase in non-gated failure rate tolerated before REJECT.
    max_failure_rate_increase: float = 0.02
    #: A mean quality delta whose CI low clears this counts as a positive
    #: *improvement* signal (vs merely non-inferior).
    improvement_ci_low: float = 0.0


class PromotionRecommendation(BaseModel):
    """The verdict + the reasons + a snapshot of the deciding numbers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_model: str
    production_model: str
    verdict: Verdict
    reasons: list[ReasonCode] = Field(default_factory=list)
    summary: str
    analysis: ComparisonAnalysis
    thresholds: PromotionThresholds

    @property
    def promote(self) -> bool:
        """True iff the verdict is :attr:`Verdict.PROMOTE`."""
        return self.verdict is Verdict.PROMOTE


def recommend(
    analysis: ComparisonAnalysis,
    thresholds: PromotionThresholds | None = None,
) -> PromotionRecommendation:
    """Apply ``thresholds`` to ``analysis`` and return a typed recommendation.

    Gate order (first decisive gate wins for HOLD/REJECT):

    1. Enough comparable samples? else **HOLD** (insufficient evidence).
    2. Quality CI low below the regression line? **REJECT**.
    3. Cost increase beyond tolerance? **REJECT**.
    4. Reliability regression beyond tolerance? **REJECT**.
    5. Quality CI inconclusive (straddles the no-worse line)? **HOLD**.
    6. Win-rate below the floor? **HOLD**.
    7. Otherwise **PROMOTE**.
    """
    th = thresholds or PromotionThresholds()
    reasons: list[ReasonCode] = []

    quality = analysis.quality
    if quality is None or quality.n_comparable < th.min_comparable_samples:
        if quality is None:
            reasons.append(ReasonCode.NO_COMPARABLE_PAIRS)
        else:
            reasons.append(ReasonCode.INSUFFICIENT_SAMPLES)
        return _build(
            analysis,
            th,
            Verdict.HOLD,
            reasons,
            "Not enough comparable paired samples for a decision; keep collecting.",
        )

    # --- REJECT gates: clear evidence of a regression. --- #
    if quality.t_ci_low < th.min_quality_ci_low:
        reasons.append(ReasonCode.QUALITY_REGRESSION)
        return _build(
            analysis,
            th,
            Verdict.REJECT,
            reasons,
            (
                f"Quality regression: mean delta {quality.mean_delta:+.4f}, "
                f"{int(quality.confidence * 100)}% CI low {quality.t_ci_low:+.4f} "
                f"below the {th.min_quality_ci_low:+.4f} bar."
            ),
        )

    if analysis.cost.mean_cost_delta_s > th.max_cost_increase_s:
        reasons.append(ReasonCode.COST_REGRESSION)
        return _build(
            analysis,
            th,
            Verdict.REJECT,
            reasons,
            (
                f"Cost regression: +{analysis.cost.mean_cost_delta_s:.3f} video-s/shot "
                f"exceeds the +{th.max_cost_increase_s:.3f}s tolerance."
            ),
        )

    if analysis.reliability.failure_rate_delta > th.max_failure_rate_increase:
        reasons.append(ReasonCode.RELIABILITY_REGRESSION)
        return _build(
            analysis,
            th,
            Verdict.REJECT,
            reasons,
            (
                f"Reliability regression: failure-rate up "
                f"{analysis.reliability.failure_rate_delta:+.3f} beyond the "
                f"{th.max_failure_rate_increase:+.3f} tolerance."
            ),
        )

    # --- HOLD gates: passed REJECT but not yet PROMOTE-worthy. --- #
    if quality.t_ci_low < th.improvement_ci_low and not quality.quality_not_worse:
        reasons.append(ReasonCode.QUALITY_INCONCLUSIVE)
        return _build(
            analysis,
            th,
            Verdict.HOLD,
            reasons,
            "Quality CI is inconclusive about non-inferiority; keep collecting.",
        )

    if quality.win_rate < th.min_win_rate:
        reasons.append(ReasonCode.WIN_RATE_BELOW_FLOOR)
        return _build(
            analysis,
            th,
            Verdict.HOLD,
            reasons,
            (
                f"Win-rate {quality.win_rate:.2%} below the {th.min_win_rate:.0%} "
                "floor; quality is non-inferior but not yet a clear win."
            ),
        )

    # --- PROMOTE: every gate cleared. --- #
    if quality.t_ci_low >= th.improvement_ci_low and quality.t_ci_low > 0.0:
        reasons.append(ReasonCode.QUALITY_IMPROVEMENT)
    else:
        reasons.append(ReasonCode.QUALITY_NON_INFERIOR)
    reasons.append(ReasonCode.ALL_GATES_PASSED)
    return _build(
        analysis,
        th,
        Verdict.PROMOTE,
        reasons,
        (
            f"Candidate cleared every gate: mean quality delta "
            f"{quality.mean_delta:+.4f} (CI low {quality.t_ci_low:+.4f}), "
            f"win-rate {quality.win_rate:.2%}, "
            f"cost {analysis.cost.mean_cost_delta_s:+.3f} video-s/shot. "
            "Safe to start a real canary."
        ),
    )


def _build(
    analysis: ComparisonAnalysis,
    thresholds: PromotionThresholds,
    verdict: Verdict,
    reasons: list[ReasonCode],
    summary: str,
) -> PromotionRecommendation:
    return PromotionRecommendation(
        candidate_model=analysis.candidate_model,
        production_model=analysis.production_model,
        verdict=verdict,
        reasons=reasons,
        summary=summary,
        analysis=analysis,
        thresholds=thresholds,
    )


__all__ = [
    "PromotionRecommendation",
    "PromotionThresholds",
    "ReasonCode",
    "Verdict",
    "recommend",
]
