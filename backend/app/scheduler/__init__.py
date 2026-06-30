"""The Scheduler / Prefetch Controller — generation-on-scroll (kinora.md §4).

The control plane that decides *what to render right now and what to leave cold*
— distinct from the creative Showrunner. It holds per-session reading state
(:class:`SchedulerSession`), classifies upcoming shots into committed/speculative/
cold zones (:mod:`app.scheduler.zones`), fills the committed buffer under
dual-watermark hysteresis with velocity-adaptive promotion
(:class:`SchedulerService`), debounces/dwell-confirms intent and handles seeks
(:class:`IntentController`), and maintains the cheap, zero-video keyframe lane
(:class:`KeyframeService`).

The **predictive prefetch** layer adds, all pure and budget-safe (promotion stays
``budget.can_render_live()``-gated):

* :class:`ReadingModel` — a per-reader online estimator of velocity, its variance,
  and dwell (:mod:`app.scheduler.prediction`);
* :func:`adapt_watermarks` — tune ``L``/``H``/``C`` to a reader's variance
  (:mod:`app.scheduler.adaptive`);
* :func:`optimize_promotions` — a budget-optimal knapsack over scarce video-seconds
  near the floor (:mod:`app.scheduler.optimizer`);
* :class:`FairShareAllocator` — split one shared budget fairly across readers
  (:mod:`app.scheduler.fairness`);
* :class:`SpeculationLedger` — speculative execution that is *undoable* on
  trajectory invalidation (:mod:`app.scheduler.rollback`);
* :func:`replay_trace` / :class:`ReaderProfile` — a deterministic, zero-video
  reading-trace simulation harness (:mod:`app.scheduler.simulation`);
* :class:`SchedulerPolicy` + :func:`run_ab` — an offline policy A/B framework
  (:mod:`app.scheduler.policy` / :mod:`app.scheduler.experiment`).
"""

from __future__ import annotations

from app.scheduler.adaptive import (
    AdaptiveConfig,
    Watermarks,
    adapt_watermarks,
    base_watermarks,
)
from app.scheduler.experiment import ABResult, ArmReport, run_ab, score_policy
from app.scheduler.fairness import Allocation, FairShareAllocator, SessionDemand
from app.scheduler.intent import IntentController, IntentResult, SeekResult
from app.scheduler.keyframe import KeyframeResult, KeyframeService
from app.scheduler.model import (
    BufferedShot,
    SchedulerSession,
    SchedulerStore,
    new_trajectory_token,
)
from app.scheduler.optimizer import (
    Candidate,
    Selection,
    build_candidate,
    optimize_promotions,
)
from app.scheduler.policy import SchedulerPolicy
from app.scheduler.prediction import ReadingModel, VelocityPrediction
from app.scheduler.rollback import (
    InvalidationReason,
    RollbackPlan,
    SpeculationLedger,
    SpeculativePromotion,
)
from app.scheduler.service import (
    QueueKeyframeMaintainer,
    SchedulerService,
    SchedulerTick,
)
from app.scheduler.simulation import (
    ActionKind,
    ReaderAction,
    ReaderProfile,
    ReadingTrace,
    SimulationResult,
    replay_trace,
)
from app.scheduler.v2 import AdaptiveStrategy  # opt-in adaptive layer (app/scheduler/v2)
from app.scheduler.zones import (
    Zone,
    clamp_velocity,
    classify,
    eta_seconds,
    trajectory_is_stable,
)

__all__ = [
    "ABResult",
    "AdaptiveStrategy",
    "ActionKind",
    "AdaptiveConfig",
    "Allocation",
    "ArmReport",
    "BufferedShot",
    "Candidate",
    "FairShareAllocator",
    "IntentController",
    "IntentResult",
    "InvalidationReason",
    "KeyframeResult",
    "KeyframeService",
    "QueueKeyframeMaintainer",
    "ReaderAction",
    "ReaderProfile",
    "ReadingModel",
    "ReadingTrace",
    "RollbackPlan",
    "SchedulerPolicy",
    "SchedulerService",
    "SchedulerSession",
    "SchedulerStore",
    "SchedulerTick",
    "SeekResult",
    "Selection",
    "SessionDemand",
    "SimulationResult",
    "SpeculationLedger",
    "SpeculativePromotion",
    "VelocityPrediction",
    "Watermarks",
    "Zone",
    "adapt_watermarks",
    "base_watermarks",
    "build_candidate",
    "clamp_velocity",
    "classify",
    "eta_seconds",
    "new_trajectory_token",
    "optimize_promotions",
    "replay_trace",
    "run_ab",
    "score_policy",
    "trajectory_is_stable",
]
