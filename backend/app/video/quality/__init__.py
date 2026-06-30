"""Provider quality scoring / auto-eval harness — one ruler for any video model.

Kinora can render with several video providers (Wan turbo/plus, MiniMax/Hailuo, a
local tester …) whose quality varies wildly. This package scores **any** provider's
clip on a single model-agnostic ruler so the router/registry can pick, route, and
rank providers:

* :class:`~app.video.quality.scores.QualityScore` — the model-agnostic ruler: six
  0..1 axes (technical integrity, aesthetic, prompt adherence, identity + style
  consistency, motion naturalness) + artifact/NSFW flags + a weighted aggregate.
* :class:`~app.video.quality.features.FrameFeatureExtractor` — pluggable frame-stat
  seam (default pure :class:`~app.video.quality.features.FrameStatsExtractor`:
  blockiness / blur / banding / temporal-flicker); a static fake for tests.
* :class:`~app.video.quality.vl_scorer.VlScorer` — pluggable VL seam for the
  perceptual / semantic axes (aesthetic, prompt adherence, NSFW); a real
  ``VLProvider`` adapter behind the seam, deterministic fakes for tests.
* :class:`~app.video.quality.evaluator.ClipEvaluator` — fuses the seams into the
  aggregate, reusing the §9.5 CCS / style-centroid *concepts* without touching the
  Critic.
* :class:`~app.video.quality.ledger.QualityLedger` — rolling, decaying per-provider
  reputation (EWMA + flag-rate + confidence shrink) that feeds router selection.
* :class:`~app.video.quality.benchmark.BenchmarkRunner` — scores a fixed clip set
  per provider and emits a comparison report + leaderboard.

It is **additive** and never imports or mutates the §9.5 Critic.
"""

from __future__ import annotations

from .benchmark import (
    BenchmarkPrompt,
    BenchmarkRunner,
    BenchmarkSuite,
    Leaderboard,
    ProviderResult,
    ProviderSubmission,
    SubmittedClip,
    merge_into_ledger,
)
from .evaluator import ClipEvaluator, ClipSample
from .features import (
    FrameFeatureExtractor,
    FrameFeatures,
    FrameStatsExtractor,
    StaticFeatureExtractor,
    banding_score,
    blockiness_score,
    blur_score,
    motion_amount_score,
    temporal_flicker_score,
)
from .ledger import (
    ProviderReputation,
    QualityLedger,
    alpha_from_half_life,
)
from .scores import (
    DEFAULT_WEIGHTS,
    FLAGGED_AGGREGATE_CAP,
    QualityScore,
    QualityWeights,
    SubScores,
    clamp01,
    weighted_aggregate,
)
from .vl_scorer import (
    RealVlScorer,
    ScriptedVlScorer,
    StaticVlScorer,
    VlScorer,
    VlVerdict,
)

__all__ = [
    "DEFAULT_WEIGHTS",
    "FLAGGED_AGGREGATE_CAP",
    "BenchmarkPrompt",
    "BenchmarkRunner",
    "BenchmarkSuite",
    "ClipEvaluator",
    "ClipSample",
    "FrameFeatureExtractor",
    "FrameFeatures",
    "FrameStatsExtractor",
    "Leaderboard",
    "ProviderReputation",
    "ProviderResult",
    "ProviderSubmission",
    "QualityLedger",
    "QualityScore",
    "QualityWeights",
    "RealVlScorer",
    "ScriptedVlScorer",
    "StaticFeatureExtractor",
    "StaticVlScorer",
    "SubScores",
    "SubmittedClip",
    "VlScorer",
    "VlVerdict",
    "alpha_from_half_life",
    "banding_score",
    "blockiness_score",
    "blur_score",
    "clamp01",
    "merge_into_ledger",
    "motion_amount_score",
    "temporal_flicker_score",
    "weighted_aggregate",
]
