"""Experiment decision report — turn raw arm counts into a ship/hold/kill call.

Given an :class:`~app.flags.experiment.Experiment` and per-arm metric observations
(success/trials for proportion metrics), this produces a single, honest
:class:`ExperimentReport`: a per-arm-vs-control comparison on the primary metric
using the **always-valid** :func:`~app.flags.stats.msprt_proportion` (so the call
is sound even though you peeked), a guardrail check on every guardrail metric,
and a recommendation enum.

It is pure: you feed it numbers, it returns a verdict. The numbers come from the
exposure log + your metric pipeline (e.g. the §13 CCS / accepted-footage /
regen-rate measurements); this module does not query anything.

Pre-registration honesty (kinora.md §13): the experiment's metrics and their
directions are declared *before* the run, and this report only ever applies the
declared test at the declared α — there is no post-hoc metric selection.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.flags.experiment import Experiment, MetricDirection, MetricKind
from app.flags.stats import (
    AlwaysValidResult,
    ProportionStat,
    msprt_proportion,
    relative_uplift,
)


class Recommendation(StrEnum):
    """The decision a report drives toward."""

    SHIP = "ship"  # a treatment beat control on the primary metric, no guardrail breach
    HOLD = "hold"  # inconclusive — keep running / collect more data
    ROLLBACK = "rollback"  # a guardrail breached, or the primary metric regressed


@dataclass(frozen=True, slots=True)
class ArmComparison:
    """One treatment arm compared to control on the primary metric."""

    variant_key: str
    control_rate: float
    treatment_rate: float
    relative_uplift: float
    stat: AlwaysValidResult
    is_winner: bool


@dataclass(frozen=True, slots=True)
class GuardrailResult:
    """A guardrail metric's verdict for one arm."""

    variant_key: str
    metric_key: str
    breached: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ExperimentReport:
    """The full decision surface for an experiment."""

    experiment_key: str
    primary_metric: str | None
    comparisons: tuple[ArmComparison, ...]
    guardrails: tuple[GuardrailResult, ...]
    recommendation: Recommendation
    rationale: str

    def to_dict(self) -> dict[str, object]:
        return {
            "experiment_key": self.experiment_key,
            "primary_metric": self.primary_metric,
            "recommendation": self.recommendation.value,
            "rationale": self.rationale,
            "comparisons": [
                {
                    "variant_key": c.variant_key,
                    "control_rate": c.control_rate,
                    "treatment_rate": c.treatment_rate,
                    "relative_uplift": c.relative_uplift,
                    "p_value": c.stat.p_value,
                    "ci_low": c.stat.ci_low,
                    "ci_high": c.stat.ci_high,
                    "decisive": c.stat.decisive,
                    "is_winner": c.is_winner,
                }
                for c in self.comparisons
            ],
            "guardrails": [
                {
                    "variant_key": g.variant_key,
                    "metric_key": g.metric_key,
                    "breached": g.breached,
                    "detail": g.detail,
                }
                for g in self.guardrails
            ],
        }


#: Per-arm metric observations: {variant_key: {metric_key: ProportionStat}}.
Observations = dict[str, dict[str, ProportionStat]]


def build_report(
    experiment: Experiment,
    observations: Observations,
    *,
    alpha: float = 0.05,
) -> ExperimentReport:
    """Compute the decision report for ``experiment`` from per-arm observations.

    Only ``PROPORTION`` metrics are evaluated (the headline §13 metrics — CCS
    pass-rate, accepted-footage fraction, regen-rate — are all proportions). The
    control arm is the experiment's declared control. The recommendation is:

    * ``ROLLBACK`` if any guardrail breached on any arm, or the best arm's primary
      metric *regressed* control decisively;
    * ``SHIP`` if at least one arm decisively *beat* control on the primary metric
      (in the metric's declared good direction) and no guardrail breached;
    * ``HOLD`` otherwise (not yet decisive).
    """
    control_key = experiment.control.key
    primary = experiment.primary_metric
    comparisons: list[ArmComparison] = []
    guardrails: list[GuardrailResult] = []

    # --- primary metric, each treatment arm vs control --------------------- #
    if primary is not None and primary.kind is MetricKind.PROPORTION:
        control_obs = observations.get(control_key, {}).get(primary.key)
        for variant in experiment.variants:
            if variant.is_control:
                continue
            treat_obs = observations.get(variant.key, {}).get(primary.key)
            if control_obs is None or treat_obs is None:
                continue
            comparisons.append(
                _compare_primary(variant.key, control_obs, treat_obs, primary.direction, alpha)
            )

    # --- guardrails, each arm ---------------------------------------------- #
    for metric in experiment.guardrails:
        if metric.kind is not MetricKind.PROPORTION:
            continue
        control_obs = observations.get(control_key, {}).get(metric.key)
        if control_obs is None:
            continue
        for variant in experiment.variants:
            if variant.is_control:
                continue
            treat_obs = observations.get(variant.key, {}).get(metric.key)
            if treat_obs is None:
                continue
            guardrails.append(
                _check_guardrail(
                    variant.key,
                    metric.key,
                    control_obs,
                    treat_obs,
                    metric.direction,
                    metric.guardrail_margin,
                    alpha,
                )
            )

    recommendation, rationale = _decide(comparisons, guardrails)
    return ExperimentReport(
        experiment_key=experiment.key,
        primary_metric=primary.key if primary is not None else None,
        comparisons=tuple(comparisons),
        guardrails=tuple(guardrails),
        recommendation=recommendation,
        rationale=rationale,
    )


def _compare_primary(
    variant_key: str,
    control: ProportionStat,
    treatment: ProportionStat,
    direction: MetricDirection,
    alpha: float,
) -> ArmComparison:
    stat = msprt_proportion(control, treatment, alpha=alpha)
    # A "win" means a decisive move in the metric's good direction.
    if direction is MetricDirection.INCREASE:
        winner = stat.decisive and stat.estimate > 0
    else:  # lower is better → control_rate - treatment_rate should be positive
        winner = stat.decisive and stat.estimate < 0
    return ArmComparison(
        variant_key=variant_key,
        control_rate=control.rate,
        treatment_rate=treatment.rate,
        relative_uplift=relative_uplift(control.rate, treatment.rate),
        stat=stat,
        is_winner=winner,
    )


def _check_guardrail(
    variant_key: str,
    metric_key: str,
    control: ProportionStat,
    treatment: ProportionStat,
    direction: MetricDirection,
    margin: float,
    alpha: float,
) -> GuardrailResult:
    # For an INCREASE-is-good guardrail, a breach is a decisive *drop* beyond the
    # tolerated margin; for DECREASE-is-good, a decisive *rise*.
    stat = msprt_proportion(control, treatment, alpha=max(alpha / 5.0, 1e-4))
    if direction is MetricDirection.INCREASE:
        tolerated = -abs(margin) * control.rate
        breached = stat.ci_high < tolerated
        detail = f"Δ={stat.estimate:+.4f} (tolerated ≥ {tolerated:.4f})"
    else:
        tolerated = abs(margin) * control.rate
        breached = stat.ci_low > tolerated
        detail = f"Δ={stat.estimate:+.4f} (tolerated ≤ {tolerated:.4f})"
    return GuardrailResult(
        variant_key=variant_key, metric_key=metric_key, breached=breached, detail=detail
    )


def _decide(
    comparisons: list[ArmComparison], guardrails: list[GuardrailResult]
) -> tuple[Recommendation, str]:
    if any(g.breached for g in guardrails):
        breached = next(g for g in guardrails if g.breached)
        return (
            Recommendation.ROLLBACK,
            f"guardrail {breached.metric_key!r} breached on arm {breached.variant_key!r}",
        )
    winners = [c for c in comparisons if c.is_winner]
    if winners:
        best = max(winners, key=lambda c: abs(c.stat.estimate))
        return (
            Recommendation.SHIP,
            f"arm {best.variant_key!r} beat control "
            f"({best.relative_uplift:+.1%}, p={best.stat.p_value:.4f})",
        )
    # A decisive *loss* on every arm is a rollback signal too.
    decisive_losses = [c for c in comparisons if c.stat.decisive and not c.is_winner]
    if comparisons and len(decisive_losses) == len(comparisons):
        return (
            Recommendation.ROLLBACK,
            "every treatment arm decisively regressed the primary metric",
        )
    return (Recommendation.HOLD, "no arm has reached a decisive result yet")


__all__ = [
    "ArmComparison",
    "ExperimentReport",
    "GuardrailResult",
    "Observations",
    "Recommendation",
    "build_report",
]
