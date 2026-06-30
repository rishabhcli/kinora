"""The facade's public request/result models + its event vocabulary.

:class:`VideoGenerationRequest` is what the render pipeline hands the facade for
one shot; :class:`VideoGenerationResult` is what comes back — a single, coherent
description of *how* the clip was produced (or why it could not be), carrying the
normalized clip, the spent video-seconds, the per-step quality/plan telemetry,
and an explicit outcome the pipeline switches on (accept vs hand-to-ffmpeg).

These are deliberately decoupled from the provider :class:`WanSpec`: the facade
*builds* the spec internally from the shot + identity conditioning + dialect
compilation, so callers think in shots, not provider request shapes.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.agents.contracts import DirectorNote, ShotSpec

from .protocols import NormalizedClip, PlanOutcome, QualityVerdict


class GenerationOutcome(StrEnum):
    """The terminal disposition of a :meth:`VideoGenerationService.generate` call."""

    #: A real provider clip was produced and passed the (optional) quality gate.
    GENERATED = "generated"
    #: The planner / budget / gate determined no real render — the caller must
    #: fall through to the ffmpeg Ken-Burns degradation lane. NOT an error.
    SKIPPED = "skipped"


class SkipReason(StrEnum):
    """Why a :meth:`generate` call returned :data:`GenerationOutcome.SKIPPED`.

    Every value is a deliberate, non-exceptional decision the caller handles by
    degrading — never a crash. ``LIVE_DISABLED`` is the spend gate; it is *not* a
    fault and never marks any provider unhealthy.
    """

    LIVE_DISABLED = "live_disabled"
    BUDGET_EXCEEDED = "budget_exceeded"
    PLANNER_SKIP = "planner_skip"
    PROVIDER_FAILED = "provider_failed"
    JOB_TIMEOUT = "job_timeout"
    QUALITY_REJECTED = "quality_rejected"


class GenerationStep(StrEnum):
    """The ordered orchestration steps the facade walks (for structured events)."""

    PLAN = "plan"
    BUDGET_RESERVE = "budget_reserve"
    SELECT_PROVIDER = "select_provider"
    CONDITION_IDENTITY = "condition_identity"
    COMPILE_PROMPT = "compile_prompt"
    SUBMIT_JOB = "submit_job"
    AWAIT_JOB = "await_job"
    NORMALIZE = "normalize"
    QUALITY_GATE = "quality_gate"
    ACCEPT = "accept"
    SKIP = "skip"


class VideoGenerationRequest(BaseModel):
    """One shot's request into the unified video-generation facade.

    The facade resolves identity, compiles the prompt, plans capability, reserves
    budget, submits/awaits the job and normalizes output — all from this. The
    caller only describes the shot + optional render context.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    shot: ShotSpec
    book_id: str
    #: Carried into budget reservation notes + telemetry; not sent to any provider.
    session_id: str | None = None
    scene_id: str | None = None
    #: Director-mode notes folded into the compiled prompt (region regen, §5.4).
    director_notes: list[DirectorNote] = Field(default_factory=list)
    #: The target clip length; defaults to the shot's own ``target_duration_s``.
    target_duration_s: float | None = None
    #: Hard override of the live gate for tests/preflight (None => use the budget).
    force_live: bool | None = None

    @property
    def duration_s(self) -> float:
        """The effective target duration (request override else the shot's)."""
        if self.target_duration_s is not None:
            return float(self.target_duration_s)
        return float(self.shot.target_duration_s)


class VideoGenerationResult(BaseModel):
    """What the facade returns for one shot — accept-or-degrade, fully explained.

    On :data:`GenerationOutcome.GENERATED` the ``clip`` is a real provider render;
    on :data:`GenerationOutcome.SKIPPED` the caller degrades to ffmpeg and
    ``skip_reason`` says why (the spend gate, budget, a provider fault, a timeout,
    or a quality reject after exhausting retries). ``video_seconds`` is the spend
    actually committed to the ledger (0 for any skip).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    shot_id: str
    outcome: GenerationOutcome
    clip: NormalizedClip | None = None
    skip_reason: SkipReason | None = None
    #: The model id that produced the clip (or the planned model on a skip).
    model: str | None = None
    #: The backend name the router routed to (telemetry / health attribution).
    provider: str | None = None
    #: Committed video-seconds (the budget-critical resource); 0 on any skip.
    video_seconds: float = 0.0
    #: How many provider attempts ran (1 happy path; >1 on quality retries).
    attempts: int = 0
    plan_outcome: PlanOutcome | None = None
    quality: QualityVerdict | None = None
    #: The provider's async task id, for cross-referencing the render in logs.
    provider_task_id: str | None = None

    @property
    def generated(self) -> bool:
        """``True`` iff a real provider clip was produced + accepted."""
        return self.outcome is GenerationOutcome.GENERATED and self.clip is not None


__all__ = [
    "GenerationOutcome",
    "GenerationStep",
    "SkipReason",
    "VideoGenerationRequest",
    "VideoGenerationResult",
]
