"""Adaptive scheduling v2 — an opt-in strategy layer over the §4 buffer.

This package is **additive**: it adds nothing to the live scheduler's behaviour
unless ``scheduler_v2_enabled`` is set (default off), and it never rewrites the
existing :mod:`app.scheduler` control loop, signatures, or spend gates. It layers
four pure planners over the dual-watermark buffer (kinora.md §4.5/§4.6/§4.9):

* :mod:`app.scheduler.v2.velocity` — a reader-velocity **regime** model
  (STEADY / SKIMMING / REREADING / PONDERING / JUMPING) that predicts the pages a
  reader will need and *sizes* the committed/speculative watermarks to the regime,
  composing with the existing §4.5 variance widening;
* :mod:`app.scheduler.v2.provider` — a multi-provider, concurrency-aware promotion
  policy that, knowing each provider's free slots and latency, fans promotions
  across the soonest-landing free slots to refill the buffer fastest;
* :mod:`app.scheduler.v2.prefetch` — a cold-zone prefetch + eviction policy
  (cheap keyframe lane only, zero video-seconds);
* :mod:`app.scheduler.v2.simulator` — a deterministic, infra-free **comparative**
  simulator that replays synthetic reader traces under a fixed-watermark baseline
  *and* the adaptive policy and reports underrun rate, wasted renders, and cost,
  proving the adaptive policy is a strict-or-equal win.

:class:`~app.scheduler.v2.strategy.AdaptiveStrategy` composes the planners into the
single opt-in object the §4.9 loop hooks. Every promotion still flows through the
caller's unchanged ``budget.can_render_live()`` gate, so nothing here can spend a
video-second the live gate would refuse.
"""

from __future__ import annotations

from app.scheduler.v2.prefetch import (
    CacheEntry,
    PrefetchCandidate,
    PrefetchConfig,
    PrefetchPlan,
    keep_value,
    plan_cold_zone,
)
from app.scheduler.v2.provider import (
    Assignment,
    Lane,
    PromotionCandidate,
    PromotionPlan,
    ProviderState,
    covers_drain,
    plan_promotions,
    total_free_slots,
)
from app.scheduler.v2.simulator import (
    Comparison,
    PolicyMetrics,
    SimShot,
    build_sim_shots,
    compare_policies,
    simulate_policy,
    standard_scenarios,
)
from app.scheduler.v2.strategy import AdaptiveStrategy
from app.scheduler.v2.velocity import (
    PageNeed,
    ReaderRegime,
    RegimeConfig,
    RegimeVerdict,
    UpcomingShot,
    VelocityRegimeModel,
    predict_pages_needed,
    size_watermarks,
)

__all__ = [
    "AdaptiveStrategy",
    "Assignment",
    "CacheEntry",
    "Comparison",
    "Lane",
    "PageNeed",
    "PolicyMetrics",
    "PrefetchCandidate",
    "PrefetchConfig",
    "PrefetchPlan",
    "PromotionCandidate",
    "PromotionPlan",
    "ProviderState",
    "ReaderRegime",
    "RegimeConfig",
    "RegimeVerdict",
    "SimShot",
    "UpcomingShot",
    "VelocityRegimeModel",
    "build_sim_shots",
    "compare_policies",
    "covers_drain",
    "keep_value",
    "plan_cold_zone",
    "plan_promotions",
    "predict_pages_needed",
    "simulate_policy",
    "size_watermarks",
    "standard_scenarios",
    "total_free_slots",
]
