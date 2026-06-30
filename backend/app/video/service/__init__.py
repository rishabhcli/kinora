"""The unified ``VideoGenerationService`` facade — the render pipeline's single
entry point to "generate this shot's clip via the best available model".

This package composes (against *local* structural protocols, so the real
round-1/2 implementations drop in unchanged) the eight video subsystems into one
coherent flow:

    plan → select-provider → condition-identity → compile-prompt
         → submit/await job → download → normalize → quality-gate → accept/skip

with budget reservation and structured events at every step. See
:mod:`app.video.service.service` for the orchestrator,
:mod:`app.video.service.assembly` for the DI composition root +
already-runnable adapters, and :mod:`app.video.service.bridge` for the drop-in
``VideoBackend`` that lets ``app/agents/generator.py`` call the facade unchanged.
"""

from __future__ import annotations

from .assembly import (
    BackendRouterAdapter,
    NullIdentityConditioner,
    PassthroughNormalizer,
    PassthroughPlanner,
    PromptPassthroughCompiler,
    RouterJobLifecycle,
    build_video_generation_service,
)
from .bridge import GeneratorBridge
from .models import (
    GenerationOutcome,
    GenerationStep,
    SkipReason,
    VideoGenerationRequest,
    VideoGenerationResult,
)
from .protocols import (
    BudgetExceededError,
    CapabilityPlanner,
    CompiledPrompt,
    CostBudget,
    CostReservation,
    EventSink,
    IdentityConditioner,
    IdentityConditioning,
    JobHandle,
    JobLifecycle,
    JobResult,
    JobStatus,
    JobTimeoutError,
    NormalizedClip,
    OutputNormalizer,
    PlanOutcome,
    PromptCompiler,
    QualityGate,
    QualityVerdict,
    VideoPlan,
    VideoRouterProtocol,
)
from .service import StructlogEventSink, VideoGenerationService

__all__ = [
    "BackendRouterAdapter",
    "BudgetExceededError",
    "CapabilityPlanner",
    "CompiledPrompt",
    "CostBudget",
    "CostReservation",
    "EventSink",
    "GenerationOutcome",
    "GenerationStep",
    "GeneratorBridge",
    "IdentityConditioner",
    "IdentityConditioning",
    "JobHandle",
    "JobLifecycle",
    "JobResult",
    "JobStatus",
    "JobTimeoutError",
    "NormalizedClip",
    "NullIdentityConditioner",
    "OutputNormalizer",
    "PassthroughNormalizer",
    "PassthroughPlanner",
    "PlanOutcome",
    "PromptCompiler",
    "PromptPassthroughCompiler",
    "QualityGate",
    "QualityVerdict",
    "RouterJobLifecycle",
    "SkipReason",
    "StructlogEventSink",
    "VideoGenerationRequest",
    "VideoGenerationResult",
    "VideoGenerationService",
    "VideoPlan",
    "VideoRouterProtocol",
    "build_video_generation_service",
]
