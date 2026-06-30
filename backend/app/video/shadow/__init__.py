"""Shadow / live-eval harness for safely evaluating a candidate video model.

Before Kinora promotes a new video model into reader traffic, it earns its place
by being evaluated against *real workloads* without ever touching what the reader
sees. This package provides that harness:

* **Shadow mode** (:mod:`.runner`) — for a sampled fraction of real render
  requests, also render the same shot on a *candidate* model **off the critical
  path**. It never blocks or alters the reader's result (the production outcome is
  an *input*), and it never charges the reader budget — candidate spend goes to a
  *separate* :class:`~app.video.shadow.budget.EvalBudget` that defaults to ZERO, so
  merely enabling shadow mode can never spend a real video-second.
* **Comparison collector** (:mod:`.collector`) — accumulates a paired-sample
  dataset (candidate vs production: quality, cost, latency, failure), keyed by
  ``shot_id`` so analysis is *paired*.
* **Paired statistics** (:mod:`.stats`) + **analysis** (:mod:`.analysis`) —
  win-rate (Wilson CI), mean quality delta (t-CI + bootstrap CI), Wilcoxon
  signed-rank, cost/latency deltas, reliability — all pure, no scipy.
* **Replay mode** (:mod:`.replay`) — re-run a recorded corpus of historical shot
  specs against a candidate, fully offline and deterministic.
* **Promotion recommendation** (:mod:`.recommendation`) — apply transparent,
  tunable thresholds to the analysis to produce a go/hold/no-go for a real canary.

Everything is over injectable seams (:mod:`.seams`: provider, quality scorer,
sampler, clock) with deterministic fakes in tests.

Design constraint (this marathon round): the real quality scorer / router / job
packages are NOT merged, so the harness depends only on *local* protocols declared
in :mod:`.seams`; the orchestrator wires the real implementations later.
"""

from __future__ import annotations

from .analysis import (
    ComparisonAnalysis,
    CostAnalysis,
    LatencyAnalysis,
    QualityAnalysis,
    ReliabilityAnalysis,
    analyze,
)
from .budget import (
    EvalBudget,
    EvalBudgetError,
    EvalBudgetExhausted,
    EvalBudgetSnapshot,
    Reservation,
)
from .clock import ManualClock, MonotonicClock
from .collector import (
    ComparisonDataset,
    FailureTally,
    PairedSample,
    candidate_failures,
    production_failures,
)
from .config import ShadowSettings, shadow_settings_from
from .recommendation import (
    PromotionRecommendation,
    PromotionThresholds,
    ReasonCode,
    Verdict,
    recommend,
)
from .replay import RecordedShot, ReplayCorpus, replay
from .runner import ShadowObservation, ShadowRunner
from .sampler import AlwaysSampler, DeterministicSampler
from .seams import (
    Clock,
    FailureKind,
    QualityScorer,
    RenderOutcome,
    Sampler,
    ShotSpec,
    VideoRenderProvider,
)

__all__ = [
    # seams + domain types
    "Clock",
    "FailureKind",
    "QualityScorer",
    "RenderOutcome",
    "Sampler",
    "ShotSpec",
    "VideoRenderProvider",
    # sampler
    "AlwaysSampler",
    "DeterministicSampler",
    # clock
    "ManualClock",
    "MonotonicClock",
    # budget
    "EvalBudget",
    "EvalBudgetError",
    "EvalBudgetExhausted",
    "EvalBudgetSnapshot",
    "Reservation",
    # collector
    "ComparisonDataset",
    "FailureTally",
    "PairedSample",
    "candidate_failures",
    "production_failures",
    # runner
    "ShadowObservation",
    "ShadowRunner",
    # analysis
    "ComparisonAnalysis",
    "CostAnalysis",
    "LatencyAnalysis",
    "QualityAnalysis",
    "ReliabilityAnalysis",
    "analyze",
    # replay
    "RecordedShot",
    "ReplayCorpus",
    "replay",
    # recommendation
    "PromotionRecommendation",
    "PromotionThresholds",
    "ReasonCode",
    "Verdict",
    "recommend",
    # config
    "ShadowSettings",
    "shadow_settings_from",
]
