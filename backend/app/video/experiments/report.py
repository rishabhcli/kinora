"""Experiment decision report — raw arm aggregates → a promote/hold/rollback call.

Given a :class:`~app.video.experiments.models.VideoExperiment` and the
:class:`~app.video.experiments.metrics.MetricCollector` that has been folding in
render outcomes, this produces one honest :class:`ExperimentReport`:

* each treatment arm compared to control on the **primary metric** (the one that
  decides the winner), using the right test for the metric kind (sequential
  two-proportion for binary, Welch for continuous);
* every **guardrail metric** checked for a direction-aware, magnitude-gated
  breach on every treatment arm;
* a single :class:`Recommendation` — PROMOTE a clear winner, ROLLBACK an arm that
  breached a guardrail (rollback always wins over promote), or HOLD when it's too
  early (below ``min_samples_per_arm``) or simply inconclusive.

It is pure: feed it the experiment + collector, get a verdict. Stopping rules
(min samples, max duration) and rollout state live in :mod:`.runner`; this module
only renders the statistical decision surface at the current data.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.video.experiments.metrics import MetricCollector
from app.video.experiments.models import MetricKind, VideoExperiment, VideoMetric
from app.video.experiments.statistics import (
    Comparison,
    GuardrailVerdict,
    compare_mean,
    compare_proportion,
    guardrail_breach,
)


class Recommendation(StrEnum):
    """The call a report drives toward."""

    PROMOTE = "promote"  # a treatment beat control on the primary metric, no breach
    HOLD = "hold"  # inconclusive or not enough data yet
    ROLLBACK = "rollback"  # a guardrail breached on some arm


@dataclass(frozen=True, slots=True)
class ArmReport:
    """The full per-arm decision surface."""

    variant_key: str
    label: str  # provider/model, for humans
    samples: int
    primary: Comparison | None
    guardrails: tuple[GuardrailVerdict, ...]
    is_winner: bool
    breached: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "variant_key": self.variant_key,
            "label": self.label,
            "samples": self.samples,
            "primary": self.primary.to_dict() if self.primary is not None else None,
            "guardrails": [
                {"metric_key": g.metric_key, "breached": g.breached, "detail": g.detail}
                for g in self.guardrails
            ],
            "is_winner": self.is_winner,
            "breached": self.breached,
        }


@dataclass(frozen=True, slots=True)
class ExperimentReport:
    """The decision surface for an entire experiment at the current data."""

    experiment_key: str
    primary_metric: str | None
    arms: tuple[ArmReport, ...]
    recommendation: Recommendation
    winner_key: str | None
    rollback_keys: tuple[str, ...]
    rationale: str

    def to_dict(self) -> dict[str, object]:
        return {
            "experiment_key": self.experiment_key,
            "primary_metric": self.primary_metric,
            "recommendation": self.recommendation.value,
            "winner_key": self.winner_key,
            "rollback_keys": list(self.rollback_keys),
            "rationale": self.rationale,
            "arms": [a.to_dict() for a in self.arms],
        }

    def render_text(self) -> str:
        """A compact human report."""
        lines = [
            f"Experiment '{self.experiment_key}': {self.recommendation.value.upper()}",
            f"  primary metric: {self.primary_metric or '(none)'}",
            f"  {self.rationale}",
        ]
        for arm in self.arms:
            mark = "WIN " if arm.is_winner else ("BAD " if arm.breached else "    ")
            p = arm.primary
            stat = (
                f"Δ={p.absolute_diff:+.4f} rel={p.relative_change:+.2%} "
                f"p={p.p_value:.4f}{' *' if p.significant else ''}"
                if p is not None
                else "(control / no primary)"
            )
            lines.append(
                f"  [{mark}] {arm.variant_key:<16} {arm.label:<28} n={arm.samples:<6} {stat}"
            )
            for g in arm.guardrails:
                if g.breached:
                    lines.append(f"        guardrail {g.metric_key}: {g.detail}")
        return "\n".join(lines)


def _compare_arm(
    metric: VideoMetric,
    collector: MetricCollector,
    control_key: str,
    treatment_key: str,
    *,
    alpha: float,
) -> Comparison:
    """Compare a treatment to control on ``metric`` using the right test."""
    if metric.kind is MetricKind.PROPORTION:
        return compare_proportion(
            metric,
            collector.proportion(control_key, metric.key),
            collector.proportion(treatment_key, metric.key),
            alpha=alpha,
        )
    return compare_mean(
        metric,
        collector.mean(control_key, metric.key),
        collector.mean(treatment_key, metric.key),
        alpha=alpha,
    )


def build_report(
    experiment: VideoExperiment,
    collector: MetricCollector,
    *,
    alpha: float = 0.05,
    guardrail_alpha: float = 0.01,
) -> ExperimentReport:
    """Produce the promote/hold/rollback decision surface at the current data.

    ``alpha`` governs the primary-metric (winner) test; ``guardrail_alpha`` the
    (typically stricter) guardrail tests. A guardrail breach on *any* arm forces
    ROLLBACK for that arm and the experiment as a whole (safety dominates). A
    PROMOTE requires a treatment to be a significant winner on the primary metric
    *and* clean on every guardrail, with both it and control past the
    ``min_samples_per_arm`` floor.
    """
    primary = experiment.primary_metric
    control_key = experiment.control.key
    control_samples = collector.sample_count(control_key)

    arm_reports: list[ArmReport] = []
    rollback_keys: list[str] = []
    winner_candidates: list[tuple[str, Comparison]] = []

    for variant in experiment.treatments:
        samples = collector.sample_count(variant.key)
        enough = (
            samples >= experiment.min_samples_per_arm
            and control_samples >= experiment.min_samples_per_arm
        )

        # Guardrails (always evaluated; breach can fire before the sample floor
        # only if its own min-sample gate passes — set high enough to be safe).
        verdicts: list[GuardrailVerdict] = []
        breached = False
        for g in experiment.guardrails:
            cmp = _compare_arm(g, collector, control_key, variant.key, alpha=guardrail_alpha)
            verdict = guardrail_breach(g, cmp, min_samples=experiment.min_samples_per_arm)
            verdicts.append(verdict)
            if verdict.breached:
                breached = True
        if breached:
            rollback_keys.append(variant.key)

        # Primary comparison.
        primary_cmp: Comparison | None = None
        is_winner = False
        if primary is not None:
            primary_cmp = _compare_arm(primary, collector, control_key, variant.key, alpha=alpha)
            if enough and not breached and primary_cmp.significant_win:
                is_winner = True
                winner_candidates.append((variant.key, primary_cmp))

        arm_reports.append(
            ArmReport(
                variant_key=variant.key,
                label=variant.describe(),
                samples=samples,
                primary=primary_cmp,
                guardrails=tuple(verdicts),
                is_winner=is_winner,
                breached=breached,
            )
        )

    recommendation, winner_key, rationale = _decide(
        experiment,
        primary,
        control_samples,
        winner_candidates,
        rollback_keys,
    )
    return ExperimentReport(
        experiment_key=experiment.key,
        primary_metric=primary.key if primary is not None else None,
        arms=tuple(arm_reports),
        recommendation=recommendation,
        winner_key=winner_key,
        rollback_keys=tuple(rollback_keys),
        rationale=rationale,
    )


def _decide(
    experiment: VideoExperiment,
    primary: VideoMetric | None,
    control_samples: int,
    winners: list[tuple[str, Comparison]],
    rollbacks: list[str],
) -> tuple[Recommendation, str | None, str]:
    """Resolve the single recommendation. Rollback dominates promote."""
    if rollbacks:
        return (
            Recommendation.ROLLBACK,
            None,
            f"{len(rollbacks)} arm(s) breached a guardrail: {', '.join(rollbacks)}",
        )
    if primary is None:
        return (Recommendation.HOLD, None, "no primary metric declared")
    if control_samples < experiment.min_samples_per_arm:
        return (
            Recommendation.HOLD,
            None,
            f"control below sample floor ({control_samples}/{experiment.min_samples_per_arm})",
        )
    if not winners:
        return (Recommendation.HOLD, None, "no treatment is a significant winner yet")
    # Promote the strongest winner by effect size in the good direction.
    best_key, best_cmp = max(winners, key=lambda kc: abs(kc[1].absolute_diff))
    return (
        Recommendation.PROMOTE,
        best_key,
        f"{best_key} significantly beats control on {primary.key} "
        f"(rel {best_cmp.relative_change:+.2%}, p={best_cmp.p_value:.4f})",
    )


__all__ = [
    "ArmReport",
    "ExperimentReport",
    "Recommendation",
    "build_report",
]
