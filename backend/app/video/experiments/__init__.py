"""A/B + progressive-canary experiment framework for Kinora video models.

New video models (a faster Wan turbo, a MiniMax Hailuo revision, a new provider)
arrive constantly. This package safely rolls one in and *proves* it is better
before it carries reader traffic, and backs it out instantly if it regresses.

Pieces (all pure, infra-free, deterministic — no clock/RNG/storage except the
injectable clock the runner takes):

* :mod:`.models` — the :class:`VideoExperiment` definition: a control vs N
  treatment :class:`VideoVariant` s (provider/model/param sets), basis-point
  allocation, :class:`Targeting`, guardrail + primary :class:`VideoMetric` s,
  minimum sample size, maximum duration.
* :mod:`.assignment` — deterministic **sticky** assignment of a
  :class:`RenderUnit` (book/shot) to a variant, reusing the platform's
  SHA-256 basis-point bucketing; the same book stays on its model, and a growing
  rollout only ever adds units (monotone ramp).
* :mod:`.metrics` — the :class:`MetricCollector` that folds :class:`RenderOutcome`
  s (quality / accept / cost / latency / failure) into streaming per-arm stats.
* :mod:`.statistics` — direction-aware comparison + guardrail-breach logic on top
  of the proven :mod:`app.flags.stats` two-proportion / Welch / mSPRT engine.
* :mod:`.report` — the promote/hold/rollback decision surface.
* :mod:`.runner` — the rollout state machine (:class:`ExperimentRunner`) with
  auto-rollback on breach and auto-promote of a winner, plus the progressive
  :class:`CanaryRunner` (1% → 5% → 25% → 100% with halt-on-regression).

The math is shared with :mod:`app.flags` (feature-flag experimentation); this
package is the video-model-specific layer — variants are render configs, metrics
are render outcomes, and the rollout drives the Generator's default model.
"""

from __future__ import annotations

from app.video.experiments.assignment import (
    RenderUnit,
    VideoAssigner,
    VideoAssignment,
)
from app.video.experiments.metrics import MetricCollector, RenderOutcome
from app.video.experiments.models import (
    ACCEPT_RATE,
    COST_PER_SECOND,
    FAILURE_RATE,
    LATENCY_MS,
    QUALITY_SCORE,
    MetricDirection,
    MetricKind,
    Targeting,
    VideoExperiment,
    VideoExperimentError,
    VideoMetric,
    VideoVariant,
    expected_allocation,
)
from app.video.experiments.report import (
    ArmReport,
    ExperimentReport,
    Recommendation,
    build_report,
)
from app.video.experiments.runner import (
    DEFAULT_CANARY_LADDER,
    CanaryRunner,
    ExperimentRunner,
    RolloutDecision,
    RolloutState,
)
from app.video.experiments.statistics import (
    Comparison,
    GuardrailVerdict,
    compare_mean,
    compare_proportion,
    guardrail_breach,
)

__all__ = [
    "ACCEPT_RATE",
    "COST_PER_SECOND",
    "DEFAULT_CANARY_LADDER",
    "FAILURE_RATE",
    "LATENCY_MS",
    "QUALITY_SCORE",
    "ArmReport",
    "CanaryRunner",
    "Comparison",
    "ExperimentReport",
    "ExperimentRunner",
    "GuardrailVerdict",
    "MetricCollector",
    "MetricDirection",
    "MetricKind",
    "Recommendation",
    "RenderOutcome",
    "RenderUnit",
    "RolloutDecision",
    "RolloutState",
    "Targeting",
    "VideoAssigner",
    "VideoAssignment",
    "VideoExperiment",
    "VideoExperimentError",
    "VideoMetric",
    "VideoVariant",
    "build_report",
    "compare_mean",
    "compare_proportion",
    "expected_allocation",
    "guardrail_breach",
]
