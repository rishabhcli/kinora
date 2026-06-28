"""Cost-ledger reconciliation (kinora.md §11.1, §12.5).

Two ledgers track spend from different angles:

* ``budget_ledger`` (``BudgetRepo``) — the **authoritative** record of the scarce
  video-seconds: ``reserve``/``commit``/``release`` rows, hard-capped.
* ``cost_ledger`` (``CostLedgerRepo``) — the **USD valuation** of *all* spend,
  including the video-seconds, recorded for attribution.

If the two disagree on video-seconds, something is mis-recorded (a render charged
the budget but never wrote a cost row, or vice-versa). :func:`reconcile` is the
pure comparison that turns the two committed-video-seconds totals into a
:class:`Reconciliation` verdict with the drift and whether it is within tolerance.

It is deliberately I/O-free: the caller (``FinOpsService``) fetches the two
totals (one from each repo) and hands them here, so this is trivially testable
and reusable across scopes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

#: Default reconciliation tolerance in video-seconds. Rounding (actual durations
#: are floats) means an exact match is unrealistic; a tenth of a second per scope
#: is comfortably below one shot.
DEFAULT_TOLERANCE_S = 0.1


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """The verdict of comparing the two ledgers' video-seconds for a scope."""

    scope_label: str
    budget_committed_s: float
    cost_recorded_s: float
    tolerance_s: float

    @property
    def drift_s(self) -> float:
        """Signed drift: cost-ledger minus budget-ledger video-seconds."""
        return self.cost_recorded_s - self.budget_committed_s

    @property
    def abs_drift_s(self) -> float:
        return abs(self.drift_s)

    @property
    def reconciled(self) -> bool:
        """True when the two ledgers agree within ``tolerance_s``."""
        return self.abs_drift_s <= self.tolerance_s + 1e-9

    def as_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope_label,
            "budget_committed_s": round(self.budget_committed_s, 3),
            "cost_recorded_s": round(self.cost_recorded_s, 3),
            "drift_s": round(self.drift_s, 4),
            "tolerance_s": self.tolerance_s,
            "reconciled": self.reconciled,
        }


def reconcile(
    *,
    scope_label: str,
    budget_committed_s: float,
    cost_recorded_s: float,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
) -> Reconciliation:
    """Compare a scope's two video-seconds totals into a :class:`Reconciliation`."""
    return Reconciliation(
        scope_label=scope_label,
        budget_committed_s=budget_committed_s,
        cost_recorded_s=cost_recorded_s,
        tolerance_s=tolerance_s,
    )


@dataclass(frozen=True, slots=True)
class CostSummary:
    """A compact USD + physical-units summary for a scope (for the API/HUD)."""

    scope_label: str
    cost_usd: Decimal
    video_seconds: float
    by_agent_usd: dict[str, Decimal]
    by_kind_usd: dict[str, Decimal]

    def as_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope_label,
            "cost_usd": str(self.cost_usd),
            "video_seconds": round(self.video_seconds, 3),
            "by_agent_usd": {a: str(v) for a, v in self.by_agent_usd.items()},
            "by_kind_usd": {k: str(v) for k, v in self.by_kind_usd.items()},
        }


__all__ = [
    "DEFAULT_TOLERANCE_S",
    "CostSummary",
    "Reconciliation",
    "reconcile",
]
