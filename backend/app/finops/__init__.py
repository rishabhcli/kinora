"""FinOps — budget & cost governance over the scarce video-seconds (kinora.md §11.1).

A layer *on top of* the load-bearing :class:`~app.memory.budget_service.BudgetService`
(reserve/commit/release over the append-only ``budget_ledger``). It never changes
that contract; it adds multi-scope tiered caps, reading-trajectory cost
forecasting, per-agent/per-shot attribution, a quality↔budget render-mode
optimizer, an auditable USD cost ledger with reconciliation, and a no-infra
simulation harness that proves the system stays inside budget.
"""

from __future__ import annotations

from app.finops.attribution import (
    Agent,
    ShotCost,
    ShotCostRecorder,
    attribute_agent,
    attribute_by_agent,
    attribute_shot,
)
from app.finops.forecast import (
    BurnDown,
    BurnSample,
    ForecastReport,
    ReadingTrajectory,
    VelocityEstimator,
    build_forecast,
    burn_down,
    forecast_video_seconds,
    seconds_to_exhaustion,
)
from app.finops.governor import GovernanceDecision, Recommendation, govern
from app.finops.ledger import CostSummary, Reconciliation, reconcile
from app.finops.optimizer import (
    OptimizationPlan,
    RenderRung,
    ShotAssignment,
    ShotOption,
    optimize,
    optimize_greedy,
)
from app.finops.service import FinOpsService, TenantUsage
from app.finops.simulation import (
    PoolResult,
    SimulationResult,
    SuiteReport,
    SyntheticReader,
    default_reader_suite,
    run_suite,
    simulate_pool,
    simulate_reader,
)
from app.finops.tiers import (
    SCOPE_ORDER,
    AlertLevel,
    BudgetScopeKind,
    BudgetTierPolicy,
    CapStatus,
    TieredCap,
    TierThresholds,
)

__all__ = [
    "SCOPE_ORDER",
    "Agent",
    "AlertLevel",
    "BudgetScopeKind",
    "BudgetTierPolicy",
    "BurnDown",
    "BurnSample",
    "CapStatus",
    "CostSummary",
    "FinOpsService",
    "ForecastReport",
    "GovernanceDecision",
    "OptimizationPlan",
    "PoolResult",
    "ReadingTrajectory",
    "Recommendation",
    "Reconciliation",
    "RenderRung",
    "ShotAssignment",
    "ShotCost",
    "ShotCostRecorder",
    "ShotOption",
    "SimulationResult",
    "SuiteReport",
    "SyntheticReader",
    "TenantUsage",
    "TierThresholds",
    "TieredCap",
    "VelocityEstimator",
    "attribute_agent",
    "attribute_by_agent",
    "attribute_shot",
    "build_forecast",
    "burn_down",
    "default_reader_suite",
    "forecast_video_seconds",
    "govern",
    "optimize",
    "optimize_greedy",
    "reconcile",
    "run_suite",
    "seconds_to_exhaustion",
    "simulate_pool",
    "simulate_reader",
]
