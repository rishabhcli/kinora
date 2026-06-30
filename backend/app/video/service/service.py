"""The unified :class:`VideoGenerationService` facade — the single entry point.

``generate(request)`` runs the whole flow for one shot through the eight composed
subsystems, in this fixed order:

    plan → reserve budget → select provider → condition identity → compile prompt
         → submit job → await job → download/normalize → quality gate
         → (accept | bounded retry | skip-to-ffmpeg)

Every step is wrapped so the budget ledger never leaks a reservation and so the
deliberate ``LiveVideoDisabled`` spend gate is a clean *skip*, not a fault. A
structured event is emitted at each step. The facade is constructed entirely from
injected protocols (see :func:`build_video_generation_service`) so the real
round-1/2 implementations drop in unchanged.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from app.agents.contracts import RenderMode, ShotSpec
from app.core.logging import get_logger
from app.providers.errors import LiveVideoDisabled, ProviderError
from app.providers.types import VideoResult, WanMode, WanSpec

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
    EventSink,
    IdentityConditioner,
    IdentityConditioning,
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

logger = get_logger("app.video.service")

#: 1:1 render-mode → Wan-mode mapping (mirrors ``app.agents.generator``); kept
#: local so the facade does not depend on the agents' generator module. The enum
#: *values* are identical by contract (§9.3), so this is a value-preserving map.
_RENDER_TO_WAN: dict[str, WanMode] = {mode.value: WanMode(mode.value) for mode in RenderMode}


def _is_budget_exceeded(exc: BaseException) -> bool:
    """Duck-typed match for the real ``BudgetExceeded`` and the local mirror.

    The facade catches budget-exceed by both the local
    :class:`~app.video.service.protocols.BudgetExceededError` and *any* exception
    whose class name is ``BudgetExceeded`` — so the production
    ``app.memory.budget_service.BudgetExceeded`` is caught without importing it.
    """
    return isinstance(exc, BudgetExceededError) or type(exc).__name__ == "BudgetExceeded"


@dataclass(frozen=True, slots=True)
class _JobOutcome:
    """Internal: one submit→await attempt's result (success carries the result)."""

    job_result: JobResult | None = None
    provider: str | None = None
    skip_reason: SkipReason | None = None


class StructlogEventSink:
    """The default :class:`EventSink`: one structlog line per orchestration step."""

    def __init__(self, name: str = "app.video.service") -> None:
        self._log = get_logger(name)

    def emit(self, event: str, **fields: object) -> None:
        self._log.info(event, **fields)


class VideoGenerationService:
    """Compose the video subsystems into one ``generate(shot)`` flow (the facade).

    All collaborators are injected as protocols. ``quality_gate`` is optional: when
    absent the first successful, normalized render is shipped unconditionally
    (matching the render pipeline's advisory-QA stance); when present a fail
    triggers a bounded retry (re-roll the seed) up to ``max_attempts`` before the
    call falls through to a SKIP the caller degrades.
    """

    def __init__(
        self,
        *,
        planner: CapabilityPlanner,
        router: VideoRouterProtocol,
        identity: IdentityConditioner,
        prompts: PromptCompiler,
        budget: CostBudget,
        jobs: JobLifecycle,
        normalizer: OutputNormalizer,
        quality_gate: QualityGate | None = None,
        events: EventSink | None = None,
        max_attempts: int = 3,
        job_timeout_s: float | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._planner = planner
        self._router = router
        self._identity = identity
        self._prompts = prompts
        self._budget = budget
        self._jobs = jobs
        self._normalizer = normalizer
        self._gate = quality_gate
        self._events = events or StructlogEventSink()
        self._max_attempts = max_attempts
        self._job_timeout_s = job_timeout_s

    # -- the single entry point --------------------------------------------- #

    async def generate(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        """Generate a clip for one shot via the best available model, or SKIP.

        Never raises for a *deliberate* non-render (the spend gate, an exhausted
        budget, a provider fault, a job timeout, or a rejected clip): those are
        :data:`GenerationOutcome.SKIPPED` results the caller degrades. It only
        propagates a truly unexpected programming error.
        """
        shot = request.shot
        duration = request.duration_s

        # 1) capability plan -------------------------------------------------- #
        live_enabled = self._live_enabled(request)
        budget_low = await self._budget.is_low()
        base_spec = self._base_spec(shot, duration)
        plan = self._planner.plan(base_spec, budget_low=budget_low, live_enabled=live_enabled)
        self._emit(
            GenerationStep.PLAN,
            request,
            outcome=plan.outcome.value,
            reason=plan.reason,
            est_video_s=round(plan.estimated_video_seconds, 3),
        )
        if not live_enabled:
            return self._skip(request, SkipReason.LIVE_DISABLED, plan)
        if plan.outcome is PlanOutcome.SKIP:
            return self._skip(request, SkipReason.PLANNER_SKIP, plan)

        # 2) condition identity + 6) compile prompt build the submit spec ----- #
        conditioning = await self._condition(request)
        compiled = self._compile(request, plan)

        # Run the budget-reserved render loop with bounded quality retries.
        return await self._render_loop(request, plan, conditioning, compiled, budget_low)

    # -- the reserve → submit → await → normalize → gate loop --------------- #

    async def _render_loop(
        self,
        request: VideoGenerationRequest,
        plan: VideoPlan,
        conditioning: IdentityConditioning,
        compiled: CompiledPrompt,
        budget_low: bool,
    ) -> VideoGenerationResult:
        last_skip = SkipReason.PROVIDER_FAILED
        last_quality: QualityVerdict | None = None
        for attempt in range(1, self._max_attempts + 1):
            spec = self._build_submit_spec(plan.spec, conditioning, compiled, attempt)

            # 2) reserve budget at the planned seconds (released on any non-accept).
            try:
                reservation = await self._budget.reserve(
                    plan.estimated_video_seconds,
                    session_id=request.session_id,
                    scene_id=request.scene_id,
                    book_id=request.book_id,
                    note=f"video.service {request.shot.shot_id} attempt {attempt}",
                )
            except Exception as exc:  # noqa: BLE001 — only budget-exceed is expected
                if _is_budget_exceeded(exc):
                    self._emit(GenerationStep.BUDGET_RESERVE, request, exceeded=True)
                    return self._skip(request, SkipReason.BUDGET_EXCEEDED, plan)
                raise
            self._emit(
                GenerationStep.BUDGET_RESERVE,
                request,
                attempt=attempt,
                reserved_s=round(reservation.video_seconds, 3),
            )

            outcome = await self._submit_and_await(request, spec, budget_low)
            if outcome.job_result is None:
                # Provider fault / timeout — already emitted; release + retry/skip.
                last_skip = outcome.skip_reason or SkipReason.PROVIDER_FAILED
                await self._budget.release(reservation)
                if last_skip is SkipReason.LIVE_DISABLED:
                    # Gate slammed shut mid-flight — a clean skip, no retry.
                    return self._skip(request, SkipReason.LIVE_DISABLED, plan)
                continue

            video = outcome.job_result.result
            assert video is not None  # JobResult.succeeded guarantees this
            clip = await self._normalize(request, video, spec)

            # 8) quality gate (optional) — a fail triggers a bounded retry.
            verdict = await self._evaluate(request, clip, spec, attempt)
            last_quality = verdict
            if not verdict.passed:
                self._emit(
                    GenerationStep.QUALITY_GATE,
                    request,
                    attempt=attempt,
                    passed=False,
                    score=round(verdict.score, 4),
                    reason=verdict.reason,
                )
                await self._budget.release(reservation)
                last_skip = SkipReason.QUALITY_REJECTED
                continue

            # Accept: commit the ACTUAL rendered seconds.
            actual = float(clip.duration_s or plan.estimated_video_seconds)
            await self._budget.commit(reservation, actual)
            self._emit(
                GenerationStep.ACCEPT,
                request,
                attempt=attempt,
                provider=outcome.provider,
                model=video.model,
                video_s=round(actual, 3),
            )
            return VideoGenerationResult(
                shot_id=request.shot.shot_id,
                outcome=GenerationOutcome.GENERATED,
                clip=clip,
                model=video.model,
                provider=outcome.provider,
                video_seconds=actual,
                attempts=attempt,
                plan_outcome=plan.outcome,
                quality=verdict,
                provider_task_id=video.provider_task_id,
            )

        # Retries exhausted on quality (or repeated provider faults).
        return self._skip(request, last_skip, plan, quality=last_quality)

    async def _submit_and_await(
        self, request: VideoGenerationRequest, spec: WanSpec, budget_low: bool
    ) -> _JobOutcome:
        """Submit + await one render job into a :class:`_JobOutcome`.

        On any non-success the ``job_result`` is ``None`` and ``skip_reason`` says
        why (the spend gate, a provider fault, or a timeout); the appropriate
        structured event is emitted here so the loop only has to branch on the
        outcome."""
        # 3) select provider (the router/job-lifecycle picks; we record the name).
        self._emit(GenerationStep.SELECT_PROVIDER, request, router=self._router.name)
        try:
            handle = await self._jobs.submit(spec, budget_low=budget_low)
        except LiveVideoDisabled:
            self._emit(GenerationStep.SUBMIT_JOB, request, live_disabled=True)
            return _JobOutcome(skip_reason=SkipReason.LIVE_DISABLED)
        except ProviderError as exc:
            self._emit(
                GenerationStep.SUBMIT_JOB,
                request,
                error=type(exc).__name__,
                retryable=exc.retryable,
            )
            return _JobOutcome(skip_reason=SkipReason.PROVIDER_FAILED)
        self._emit(
            GenerationStep.SUBMIT_JOB, request, job_id=handle.job_id, provider=handle.provider
        )

        try:
            result = await self._jobs.await_result(handle, timeout_s=self._job_timeout_s)
        except JobTimeoutError:
            self._emit(GenerationStep.AWAIT_JOB, request, timeout=True, job_id=handle.job_id)
            await self._best_effort_cancel(handle)
            return _JobOutcome(skip_reason=SkipReason.JOB_TIMEOUT, provider=handle.provider)
        except LiveVideoDisabled:
            self._emit(GenerationStep.AWAIT_JOB, request, live_disabled=True)
            return _JobOutcome(skip_reason=SkipReason.LIVE_DISABLED, provider=handle.provider)
        except ProviderError as exc:
            self._emit(GenerationStep.AWAIT_JOB, request, error=type(exc).__name__)
            return _JobOutcome(skip_reason=SkipReason.PROVIDER_FAILED, provider=handle.provider)

        if result.status is JobStatus.TIMEOUT:
            self._emit(GenerationStep.AWAIT_JOB, request, timeout=True, job_id=handle.job_id)
            return _JobOutcome(skip_reason=SkipReason.JOB_TIMEOUT, provider=handle.provider)
        if not result.succeeded:
            self._emit(
                GenerationStep.AWAIT_JOB,
                request,
                status=result.status.value,
                error=result.error,
            )
            return _JobOutcome(skip_reason=SkipReason.PROVIDER_FAILED, provider=handle.provider)

        self._emit(
            GenerationStep.AWAIT_JOB, request, status=result.status.value, job_id=handle.job_id
        )
        return _JobOutcome(job_result=result, provider=handle.provider)

    async def _best_effort_cancel(self, handle: object) -> None:
        with contextlib.suppress(ProviderError, JobTimeoutError):
            await self._jobs.cancel(handle)  # type: ignore[arg-type]

    # -- step helpers -------------------------------------------------------- #

    async def _condition(self, request: VideoGenerationRequest) -> IdentityConditioning:
        conditioning = await self._identity.resolve(request.shot)
        self._emit(
            GenerationStep.CONDITION_IDENTITY,
            request,
            refs=len(conditioning.reference_image_urls),
            has_prev_frame=conditioning.image_url is not None,
            identity_hash=conditioning.identity_hash,
        )
        return conditioning

    def _compile(self, request: VideoGenerationRequest, plan: VideoPlan) -> CompiledPrompt:
        model = plan.spec.model
        compiled = self._prompts.compile(request.shot, provider=self._router.name, model=model)
        self._emit(GenerationStep.COMPILE_PROMPT, request, dialect=compiled.dialect)
        return compiled

    async def _normalize(
        self, request: VideoGenerationRequest, video: VideoResult, spec: WanSpec
    ) -> NormalizedClip:
        clip = await self._normalizer.normalize(video, spec=spec)
        self._emit(
            GenerationStep.NORMALIZE,
            request,
            duration_s=round(clip.duration_s, 3),
            has_bytes=clip.clip_bytes is not None,
            has_last_frame=clip.last_frame_bytes is not None,
        )
        return clip

    async def _evaluate(
        self, request: VideoGenerationRequest, clip: NormalizedClip, spec: WanSpec, attempt: int
    ) -> QualityVerdict:
        if self._gate is None:
            return QualityVerdict(passed=True, score=1.0)
        verdict = await self._gate.evaluate(clip, shot=request.shot, spec=spec, attempt=attempt)
        if verdict.passed:
            self._emit(
                GenerationStep.QUALITY_GATE,
                request,
                attempt=attempt,
                passed=True,
                score=round(verdict.score, 4),
            )
        return verdict

    # -- spec construction (pure) ------------------------------------------- #

    def _base_spec(self, shot: ShotSpec, duration: float) -> WanSpec:
        """A bare :class:`WanSpec` for the planner (no identity/dialect yet)."""
        return WanSpec(
            mode=_RENDER_TO_WAN[shot.render_mode.value],
            prompt=shot.prompt,
            negative_prompt=shot.negative_prompt,
            seed=shot.seed,
            duration_s=int(round(duration)),
            shot_id=shot.shot_id,
        )

    @staticmethod
    def _build_submit_spec(
        planned: WanSpec,
        conditioning: IdentityConditioning,
        compiled: CompiledPrompt,
        attempt: int,
    ) -> WanSpec:
        """Fold identity URLs + the compiled prompt into the planned spec.

        On a retry (``attempt > 1``) the seed is re-rolled deterministically so a
        quality-rejected clip is not re-rendered identically.
        """
        seed = planned.seed
        if attempt > 1 and seed is not None:
            # Deterministic re-roll: a stable LCG step keeps tests reproducible.
            seed = (seed * 1103515245 + 12345 + attempt) & 0x7FFFFFFF
        update: dict[str, object] = {
            "prompt": compiled.prompt,
            "negative_prompt": compiled.negative_prompt,
            "seed": seed,
        }
        mode = planned.mode
        if mode is WanMode.REFERENCE_TO_VIDEO:
            update["reference_image_urls"] = list(conditioning.reference_image_urls)
            if conditioning.reference_voice_url:
                update["reference_voice_url"] = conditioning.reference_voice_url
        elif mode in (WanMode.IMAGE_TO_VIDEO, WanMode.VIDEO_CONTINUATION):
            update["image_url"] = conditioning.image_url
            if conditioning.source_video_url:
                update["source_video_url"] = conditioning.source_video_url
        elif mode is WanMode.FIRST_LAST_FRAME:
            update["first_frame_url"] = conditioning.first_frame_url
            update["last_frame_url"] = conditioning.last_frame_url
        elif mode is WanMode.INSTRUCTION_EDIT:
            update["source_video_url"] = conditioning.source_video_url
        return planned.model_copy(update=update)

    # -- skip / events ------------------------------------------------------- #

    def _live_enabled(self, request: VideoGenerationRequest) -> bool:
        if request.force_live is not None:
            return request.force_live
        return self._budget.can_render_live()

    def _skip(
        self,
        request: VideoGenerationRequest,
        reason: SkipReason,
        plan: VideoPlan,
        *,
        quality: QualityVerdict | None = None,
    ) -> VideoGenerationResult:
        self._emit(GenerationStep.SKIP, request, reason=reason.value)
        return VideoGenerationResult(
            shot_id=request.shot.shot_id,
            outcome=GenerationOutcome.SKIPPED,
            skip_reason=reason,
            model=plan.spec.model,
            provider=self._router.name,
            video_seconds=0.0,
            attempts=0,
            plan_outcome=plan.outcome,
            quality=quality,
        )

    def _emit(
        self, step: GenerationStep, request: VideoGenerationRequest, **fields: object
    ) -> None:
        self._events.emit(
            f"video.service.{step.value}",
            shot_id=request.shot.shot_id,
            book_id=request.book_id,
            session_id=request.session_id,
            **fields,
        )


__all__ = ["StructlogEventSink", "VideoGenerationService"]
