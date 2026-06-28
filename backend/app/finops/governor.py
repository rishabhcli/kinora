"""The budget governor — the pure governance brain (kinora.md §11.1, §4.4, §4.6).

Given a snapshot of current usage across scopes, the active tiered-cap policy, a
reading trajectory, and the set of upcoming shots, the governor produces one
:class:`GovernanceDecision`:

* the per-scope :class:`~app.finops.tiers.CapStatus` and the worst alert level;
* the binding scope (least headroom) and whether the soft cap is crossed;
* a cost :class:`~app.finops.forecast.ForecastReport` (will the forward read fit?);
* a quality↔budget :class:`~app.finops.optimizer.OptimizationPlan` over the
  upcoming shots, sized to the *binding* headroom so it never over-commits;
* a recommendation: ``promote`` (spend freely), ``optimize`` (degrade per the
  plan to stay under soft), or ``halt`` (hard cap / floor reached — ride the
  keyframe ladder, §12.4).

It is pure (no I/O, no clock): the live numbers are passed in by the service.
This is what makes the whole governance loop unit-testable with no infrastructure
and is the core of the §11.1 "spend tokens to save video-seconds" discipline.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from app.finops.forecast import ForecastReport, ReadingTrajectory, build_forecast
from app.finops.optimizer import (
    OptimizationPlan,
    RenderRung,
    ShotOption,
    optimize,
)
from app.finops.tiers import (
    AlertLevel,
    BudgetScopeKind,
    BudgetTierPolicy,
    CapStatus,
)


class Recommendation(enum.StrEnum):
    """What the governor advises the scheduler/render path to do next."""

    PROMOTE = "promote"  # plenty of headroom — spend freely on full video
    OPTIMIZE = "optimize"  # soft cap crossed — degrade per the optimizer plan
    HALT = "halt"  # hard cap / no headroom — ride the keyframe ladder (§12.4)


@dataclass(frozen=True, slots=True)
class GovernanceDecision:
    """The governor's full verdict for one snapshot."""

    statuses: tuple[CapStatus, ...]
    worst_level: AlertLevel
    binding: CapStatus | None
    forecast: ForecastReport
    plan: OptimizationPlan
    recommendation: Recommendation

    @property
    def binding_headroom_s(self) -> float:
        """Headroom of the binding scope (``inf`` when no finite cap binds)."""
        if self.binding is None:
            return float("inf")
        return self.binding.headroom_s

    def as_dict(self) -> dict[str, object]:
        return {
            "recommendation": self.recommendation.value,
            "worst_level": self.worst_level.label,
            "binding_scope": self.binding.scope.value if self.binding else None,
            "binding_headroom_s": (
                None
                if self.binding is None
                else round(self.binding.headroom_s, 3)
            ),
            "statuses": [s.as_dict() for s in self.statuses],
            "forecast": self.forecast.as_dict(),
            "plan": self.plan.as_dict(),
        }


def _recommend(worst: AlertLevel, *, forecast_fits: bool) -> Recommendation:
    """Map the worst alert level + forecast fit to a recommendation."""
    if worst >= AlertLevel.HARD_CAP:
        return Recommendation.HALT
    if worst >= AlertLevel.SOFT_CAP or not forecast_fits:
        return Recommendation.OPTIMIZE
    return Recommendation.PROMOTE


def govern(
    policy: BudgetTierPolicy,
    *,
    used_by_scope: dict[BudgetScopeKind, float],
    trajectory: ReadingTrajectory,
    upcoming: list[ShotOption],
    horizon_s: float,
    min_quality: float = 0.0,
) -> GovernanceDecision:
    """Produce the :class:`GovernanceDecision` for one snapshot.

    The optimizer is sized to the *binding* scope's headroom so a session/scene
    cap (not just the global ceiling) constrains the plan — exactly the §11.1
    "no one reading session drains the pool" guarantee. With no finite binding
    scope the plan gets the global headroom.
    """
    statuses = tuple(policy.evaluate_all(used_by_scope))
    worst = BudgetTierPolicy.worst_level(list(statuses))
    binding = BudgetTierPolicy.binding_scope(list(statuses))

    forecast = build_forecast(
        trajectory,
        remaining_s=binding.headroom_s if binding is not None else _global_headroom(statuses),
        horizon_s=horizon_s,
    )

    plan_budget = binding.headroom_s if binding is not None else _global_headroom(statuses)
    plan = optimize(upcoming, budget_s=plan_budget, min_quality=min_quality)

    recommendation = _recommend(worst, forecast_fits=forecast.fits)
    # When halting, force every upcoming shot off full video (ride the ladder).
    if recommendation is Recommendation.HALT and plan.full_video_count:
        plan = optimize(upcoming, budget_s=0.0, min_quality=min_quality)

    return GovernanceDecision(
        statuses=statuses,
        worst_level=worst,
        binding=binding,
        forecast=forecast,
        plan=plan,
        recommendation=recommendation,
    )


def _global_headroom(statuses: tuple[CapStatus, ...]) -> float:
    """Headroom of the global scope if present, else +inf."""
    for s in statuses:
        if s.scope is BudgetScopeKind.GLOBAL:
            return s.headroom_s
    return float("inf")


def rung_priority_order() -> tuple[RenderRung, ...]:
    """The ladder rungs in fidelity order — handy for callers stepping down."""
    return tuple(sorted(RenderRung, key=lambda r: r.rank))


__all__ = [
    "GovernanceDecision",
    "Recommendation",
    "govern",
    "rung_priority_order",
]
