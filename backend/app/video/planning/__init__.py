"""Capability-aware video request planner + degradation negotiator.

Turn a desired, provider-neutral :class:`CanonicalVideoRequest` into a concrete,
*always-runnable* :class:`RenderPlan` for a target backend by automatically
translating and downgrading against the backend's declared
:class:`CapabilityProfile` — synthesize-keyframe-then-i2v when reference-to-video
is missing, chain continuation segments when a take is too long, clamp
fps/resolution/aspect to the nearest supported value, drop an unsupported seed,
compress an over-long prompt, etc. Every concession is scored into a
``fidelity_cost`` and explained in a human-readable ``rationale`` so a router can
compare providers with :func:`best_plan` / :func:`rank_plans`.

Pure planning logic: no network, no I/O, no env, no RNG — exhaustively testable.

Typical use::

    from app.video.planning import CanonicalVideoRequest, VideoMode, plan, wan_profile

    req = CanonicalVideoRequest(mode=VideoMode.REFERENCE_TO_VIDEO, duration_s=12)
    render_plan = plan(req, wan_profile())
    for step in render_plan.steps:
        ...  # execute step.mode with step.image_url / step.duration_s / ...
    print(render_plan.rationale, render_plan.fidelity_cost)
"""

from __future__ import annotations

from .capabilities import (
    ASPECT_1_1,
    ASPECT_4_3,
    ASPECT_9_16,
    ASPECT_16_9,
    ASPECT_21_9,
    AspectRatio,
    CapabilityProfile,
    VideoMode,
    resolution_height,
    resolution_pixels,
)
from .plan import (
    DEFAULT_PENALTY_WEIGHTS,
    CanonicalVideoRequest,
    FidelityNote,
    FidelityPenalty,
    PlanStep,
    PostOp,
    RenderPlan,
    StepKind,
)
from .planner import compress_prompt, plan
from .presets import minimax_profile, text_only_profile, wan_profile
from .select import best_plan, plan_all, rank_plans

__all__ = [
    "ASPECT_16_9",
    "ASPECT_1_1",
    "ASPECT_21_9",
    "ASPECT_4_3",
    "ASPECT_9_16",
    "DEFAULT_PENALTY_WEIGHTS",
    "AspectRatio",
    "CanonicalVideoRequest",
    "CapabilityProfile",
    "FidelityNote",
    "FidelityPenalty",
    "PlanStep",
    "PostOp",
    "RenderPlan",
    "StepKind",
    "VideoMode",
    "best_plan",
    "compress_prompt",
    "minimax_profile",
    "plan",
    "plan_all",
    "rank_plans",
    "resolution_height",
    "resolution_pixels",
    "text_only_profile",
    "wan_profile",
]
