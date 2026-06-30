"""The release-gate signal: "is there error budget to ship / raise a canary?".

Error budgets are a *currency* for risk. When the budget is healthy, the team
can spend it on releases and canary ramps; when it's exhausted (or burning fast)
the policy says **freeze** — stop shipping, focus on reliability. This module
turns the live SLO status into a single machine-readable gate decision the
experiment / feature-flag / canary systems consult before they raise exposure.

The decision is a three-way :class:`GateDecision`:

* ``allow`` — budget healthy, no fast burn: ship freely.
* ``caution`` — budget low (below the caution floor) **or** a slow-burn ticket
  is open: allow only low-risk changes; the canary system should ramp slower.
* ``freeze`` — any objective's budget exhausted **or** a fast-burn page is
  firing: block new releases / canary promotions.

Pure function of an already-computed :class:`~app.slo.engine.SLOStatus`, so it's
trivially testable and the flag/experiment systems can call it without I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.slo.objectives import AlertSeverity


class GateDecision(StrEnum):
    """The release-gate verdict, increasing in restriction."""

    ALLOW = "allow"
    CAUTION = "caution"
    FREEZE = "freeze"


_RANK = {GateDecision.ALLOW: 0, GateDecision.CAUTION: 1, GateDecision.FREEZE: 2}


@dataclass(frozen=True, slots=True)
class GateConfig:
    """Thresholds for the gate decision (additive, settings-tunable)."""

    #: Budget remaining (fraction) below which the gate drops to CAUTION.
    caution_floor: float = 0.25
    #: A slow-burn TICKET drops the gate to at least CAUTION when True.
    ticket_implies_caution: bool = True


@dataclass(frozen=True, slots=True)
class GateResult:
    """The release-gate decision plus the reasons that drove it."""

    decision: GateDecision
    reasons: tuple[str, ...]
    #: The minimum budget-remaining fraction across objectives (context).
    min_budget_remaining: float

    @property
    def can_release(self) -> bool:
        """True unless the gate is FROZEN (CAUTION still allows low-risk ships)."""
        return self.decision is not GateDecision.FREEZE

    @property
    def can_promote_canary(self) -> bool:
        """A canary ramp needs a clean ALLOW (no caution, no freeze)."""
        return self.decision is GateDecision.ALLOW

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "can_release": self.can_release,
            "can_promote_canary": self.can_promote_canary,
            "min_budget_remaining": round(self.min_budget_remaining, 6),
            "reasons": list(self.reasons),
        }


def decide_gate(status: object, *, config: GateConfig | None = None) -> GateResult:
    """Compute the release gate from a :class:`~app.slo.engine.SLOStatus`.

    Imported lazily-typed (``object``) to avoid an import cycle with the engine;
    the engine calls this with its own status object.
    """
    from app.slo.engine import SLOStatus

    assert isinstance(status, SLOStatus)
    cfg = config or GateConfig()

    decision = GateDecision.ALLOW
    reasons: list[str] = []

    min_remaining = 1.0
    for budget in status.budgets:
        # Only finite, real budgets matter for the floor; an empty objective
        # (no traffic) reads full budget and never freezes a release.
        remaining = budget.remaining_fraction
        if remaining < min_remaining:
            min_remaining = remaining
        if budget.is_exhausted:
            decision = _max(decision, GateDecision.FREEZE)
            reasons.append(f"budget exhausted: {budget.objective.name}")
        elif remaining < cfg.caution_floor:
            decision = _max(decision, GateDecision.CAUTION)
            reasons.append(
                f"budget low: {budget.objective.name} "
                f"({remaining * 100:.1f}% < {cfg.caution_floor * 100:.0f}%)"
            )

    for alert in status.alerts:
        if alert.severity is AlertSeverity.PAGE:
            decision = _max(decision, GateDecision.FREEZE)
            reasons.append(f"fast-burn page: {alert.objective_name}")
        elif alert.severity is AlertSeverity.TICKET and cfg.ticket_implies_caution:
            decision = _max(decision, GateDecision.CAUTION)
            reasons.append(f"slow-burn ticket: {alert.objective_name}")

    if not reasons:
        reasons.append("all objectives healthy; budget above caution floor")

    return GateResult(
        decision=decision,
        reasons=tuple(reasons),
        min_budget_remaining=min_remaining,
    )


def _max(a: GateDecision, b: GateDecision) -> GateDecision:
    return a if _RANK[a] >= _RANK[b] else b


__all__ = ["GateConfig", "GateDecision", "GateResult", "decide_gate"]
