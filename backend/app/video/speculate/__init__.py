"""Speculative pre-generation for the ahead-of-reader buffer (kinora.md §4.4/§4.6/§4.8).

Kinora renders a few seconds *ahead* of the reader. The scheduler's
committed/speculative/cold zones (``app.scheduler``) decide *when* a shot crosses a
horizon; this subsystem decides *which* speculative shots are worth pre-rendering
*now* and *at what model*, so the buffer-hit rate is maximised while wasted spend is
bounded.

The pipeline, all pure policy over injectable seams (clock / cost / cache / budget —
see :mod:`app.video.speculate.protocols`):

* :class:`~app.video.speculate.predictor.ReachPredictor` — branching-path reach
  prediction (linear advance vs. likely jump), each upcoming shot tagged with a
  calibrated hit-probability and ETA.
* :class:`~app.video.speculate.planner.PortfolioPlanner` — an expected-value, 0/1
  knapsack portfolio optimiser that maximises ``Σ P(hit)·value`` under a hard
  speculative spend cap, weighing each shot's hit-value against its ``P(waste)·cost``.
* :mod:`~app.video.speculate.cost` — per-model cost/latency awareness and the
  probability→model routing that reserves premium ids for high-probability shots
  and spends cheap turbo ids on long-shots.
* :class:`~app.video.speculate.cancellation.SpeculationLedger` — path-invalidation
  cancellation with exactly-once refund of unstarted reservations and cache salvage.
* :class:`~app.video.speculate.accounting.SpeculationAccountant` — a hit/waste
  feedback loop that tunes a bounded aggressiveness multiplier.
* :class:`~app.video.speculate.engine.SpeculationEngine` — the per-session
  orchestrator wiring them together.

This subsystem is **additive and standalone**: it never enables live video, never
spends past its injected budget seam, and imports no other Kinora subsystem — a
composition root adapts the real cost/cache/budget services onto its protocols.
"""

from __future__ import annotations

from app.video.speculate.accounting import (
    SpeculationAccountant,
    SpeculationStats,
    TunerPolicy,
)
from app.video.speculate.budget import (
    InMemorySpeculativeBudget,
    NullCache,
    SetCache,
)
from app.video.speculate.cancellation import (
    SpeculationEntry,
    SpeculationLedger,
    SpeculationStatus,
)
from app.video.speculate.cost import (
    ModelSpec,
    RoutingPolicy,
    TieredCostModel,
    class_for_probability,
    route_model_for_probability,
)
from app.video.speculate.engine import (
    EngineConfig,
    LaunchResult,
    SpeculationEngine,
)
from app.video.speculate.planner import PlannerPolicy, PortfolioPlanner
from app.video.speculate.predictor import ReachModel, ReachPredictor
from app.video.speculate.protocols import (
    CacheLookupProtocol,
    CostModelProtocol,
    SpeculativeBudgetProtocol,
)
from app.video.speculate.types import (
    CancellationOutcome,
    ModelClass,
    PathKind,
    PredictedReach,
    ReaderState,
    SpeculationChoice,
    SpeculationPlan,
    UpcomingShot,
)

__all__ = [
    "CacheLookupProtocol",
    "CancellationOutcome",
    "CostModelProtocol",
    "EngineConfig",
    "InMemorySpeculativeBudget",
    "LaunchResult",
    "ModelClass",
    "ModelSpec",
    "NullCache",
    "PathKind",
    "PlannerPolicy",
    "PortfolioPlanner",
    "PredictedReach",
    "ReachModel",
    "ReachPredictor",
    "ReaderState",
    "RoutingPolicy",
    "SetCache",
    "SpeculationAccountant",
    "SpeculationChoice",
    "SpeculationEngine",
    "SpeculationEntry",
    "SpeculationLedger",
    "SpeculationPlan",
    "SpeculationStats",
    "SpeculationStatus",
    "SpeculativeBudgetProtocol",
    "TieredCostModel",
    "TunerPolicy",
    "UpcomingShot",
    "class_for_probability",
    "route_model_for_probability",
]
