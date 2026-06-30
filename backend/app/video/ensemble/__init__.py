"""Multi-model best-of-N rendering: render a shot on K models, score, pick the winner.

For a *hero* shot the best result may come from any of several video models. This
subsystem renders the same shot on a selected set of providers (concurrently, bounded),
scores each output with the §9.5 quality axes, and selects a winner under a configurable
objective — while a strict, fail-closed budget guard keeps the K× spend from ever
happening by accident (§11).

Public surface
--------------
* :class:`BestOfNRenderer` — the orchestrator: fan-out, score, early-stop, select, settle.
* :class:`MultiRenderBudgetGuard` / :class:`FanOutDecision` / :class:`CostCapExceeded` —
  the strict budget gate (disabled-by-default, tier-gated, live-gated, cost-capped).
* :class:`EnsembleConfig` / :class:`Objective` / :class:`CostUnit` — the run tunables.
* :class:`ProviderChoice` / :class:`ShotRenderSpec` / :class:`RenderOutput` /
  :class:`QualityScore` — the data passed in/out.
* :class:`Candidate` / :class:`CandidateStatus` / :class:`SelectionReport` — the outcome
  records and the emitted "why the winner won" report.
* :class:`EnsembleProvider` / :class:`QualityScorer` / :class:`MultiRenderBudget` — the
  local structural seams (no cross-round imports; shaped to match the real types).
* :func:`select_winner` / :func:`quality_per_cost` / :func:`is_good_enough` — the pure
  selection primitives, exposed for direct use/testing.

Nothing here renders unless explicitly enabled for a shot tier *and* the live-video
gate is on; the defaults never fan out and never spend.
"""

from __future__ import annotations

from .budget_guard import (
    CostCapExceeded,
    FanOutDecision,
    FanOutRefusal,
    MultiRenderBudgetGuard,
)
from .models import (
    BudgetReservation,
    Candidate,
    CandidateStatus,
    CostUnit,
    EnsembleConfig,
    Objective,
    ProviderChoice,
    QualityScore,
    RenderOutput,
    SelectionReport,
    ShotRenderSpec,
)
from .objectives import (
    eligible_candidates,
    explain_winner,
    is_good_enough,
    quality_per_cost,
    select_winner,
    selectable,
    within_cost_cap,
)
from .protocols import EnsembleProvider, MultiRenderBudget, QualityScorer
from .renderer import BestOfNRenderer

__all__ = [
    "BestOfNRenderer",
    "BudgetReservation",
    "Candidate",
    "CandidateStatus",
    "CostCapExceeded",
    "CostUnit",
    "EnsembleConfig",
    "EnsembleProvider",
    "FanOutDecision",
    "FanOutRefusal",
    "MultiRenderBudget",
    "MultiRenderBudgetGuard",
    "Objective",
    "ProviderChoice",
    "QualityScore",
    "QualityScorer",
    "RenderOutput",
    "SelectionReport",
    "ShotRenderSpec",
    "eligible_candidates",
    "explain_winner",
    "is_good_enough",
    "quality_per_cost",
    "select_winner",
    "selectable",
    "within_cost_cap",
]
