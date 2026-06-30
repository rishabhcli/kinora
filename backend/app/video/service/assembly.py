"""DI assembly for the :class:`VideoGenerationService` + back-compat adapters.

:func:`build_video_generation_service` is the clean composition root: it takes
the eight composed subsystems as injected protocols and returns a wired facade.
Because the parameters are the *local protocols*, the real round-1/2
implementations (``app.video.routing.VideoRouter``, ``app.video.planning`` planner,
``app.video.cost`` budget, ``app.video.identity`` conditioner, ``app.video.jobs``
lifecycle, ``app.video.prompts`` compiler, ``app.video.normalize`` normalizer) drop
in unchanged at final integration.

For the *current* repo — where those packages aren't present — this module also
provides thin **adapters** over the already-merged seams so the facade is fully
runnable today against ``app.providers`` + ``app.memory.budget_service``:

* :class:`BackendRouterAdapter` — wraps any
  :class:`app.providers.video_router.VideoBackend` (a single
  :class:`~app.providers.video.VideoProvider` *or* a multi-provider
  :class:`~app.providers.video_router.VideoRouter`) as a
  :class:`~app.video.service.protocols.VideoRouterProtocol`.
* :class:`RouterJobLifecycle` — a synchronous-await job lifecycle whose
  ``submit``+``await_result`` collapse onto a single router ``render`` call (the
  hosted Wan provider already owns submit→poll→download internally). A real
  round-2 ``app.video.jobs`` lifecycle replaces this with durable, resumable jobs.
* :class:`PassthroughNormalizer` — turns a provider :class:`VideoResult` into a
  :class:`~app.video.service.protocols.NormalizedClip` with no re-encode (the
  provider already returns mp4 bytes); a real normalizer probes/transcodes.
* :class:`NullIdentityConditioner` / :class:`PromptPassthroughCompiler` /
  :class:`PassthroughPlanner` — neutral defaults so the facade runs before the
  real identity/dialect/planning subsystems are wired.
"""

from __future__ import annotations

from app.providers.types import VideoResult, WanSpec

from .protocols import (
    CapabilityPlanner,
    CompiledPrompt,
    CostBudget,
    EventSink,
    IdentityConditioner,
    IdentityConditioning,
    JobHandle,
    JobLifecycle,
    JobResult,
    JobStatus,
    NormalizedClip,
    OutputNormalizer,
    PlanOutcome,
    PromptCompiler,
    QualityGate,
    VideoPlan,
    VideoRouterProtocol,
)
from .service import VideoGenerationService

# --------------------------------------------------------------------------- #
# Adapters over the already-merged seams (runnable today)
# --------------------------------------------------------------------------- #


class BackendRouterAdapter:
    """Adapt a plain ``VideoBackend`` (or ``VideoRouter``) to the router protocol.

    A bare :class:`~app.providers.video_router.VideoBackend.render` takes no
    ``budget_low`` keyword, while a :class:`~app.providers.video_router.VideoRouter`
    accepts one. This adapter calls ``render(spec, budget_low=...)`` when the
    underlying object's signature accepts it (a router) and falls back to
    ``render(spec)`` otherwise (a single provider) — so either drops in.
    """

    def __init__(self, backend: object, *, name: str | None = None) -> None:
        self._backend = backend
        self.name = name or getattr(backend, "name", "video-backend")

    async def render(self, spec: WanSpec, *, budget_low: bool = False) -> VideoResult:
        render = self._backend.render  # type: ignore[attr-defined]
        try:
            return await render(spec, budget_low=budget_low)
        except TypeError:
            # A single VideoProvider.render(spec) has no budget_low kwarg.
            return await render(spec)

    async def healthy(self) -> bool:
        probe = getattr(self._backend, "healthy", None)
        if probe is None:
            return True
        return bool(await probe())


class RouterJobLifecycle:
    """A degenerate job lifecycle that collapses submit→await onto one render.

    The hosted Wan provider already owns the async submit→poll→download cycle
    *inside* its ``render`` call, so for the current repo "submit a job" simply
    runs the render eagerly and stashes the result; "await" returns it. A real
    round-2 ``app.video.jobs`` lifecycle (durable handle, resumable poll, external
    cancel) replaces this without touching the facade.
    """

    def __init__(self, router: VideoRouterProtocol) -> None:
        self._router = router
        self._results: dict[str, VideoResult] = {}
        self._counter = 0

    async def submit(self, spec: WanSpec, *, budget_low: bool = False) -> JobHandle:
        self._counter += 1
        job_id = f"{self._router.name}:{spec.shot_id or 'shot'}:{self._counter}"
        # The provider render raises (LiveVideoDisabled / ProviderError) here; the
        # facade catches those at submit time and skips/retries accordingly.
        self._results[job_id] = await self._router.render(spec, budget_low=budget_low)
        return JobHandle(job_id=job_id, provider=self._router.name)

    async def await_result(self, handle: JobHandle, *, timeout_s: float | None = None) -> JobResult:
        result = self._results.pop(handle.job_id, None)
        if result is None:
            return JobResult(status=JobStatus.FAILED, error="unknown job handle")
        return JobResult(status=JobStatus.SUCCEEDED, result=result)

    async def cancel(self, handle: JobHandle) -> None:
        self._results.pop(handle.job_id, None)


class PassthroughNormalizer:
    """Wrap a provider :class:`VideoResult` as a :class:`NormalizedClip` (no re-encode)."""

    async def normalize(self, result: VideoResult, *, spec: WanSpec) -> NormalizedClip:
        return NormalizedClip(
            clip_bytes=result.clip_bytes,
            clip_url=result.clip_url,
            last_frame_bytes=result.last_frame_bytes,
            duration_s=float(result.duration_s),
        )


class NullIdentityConditioner:
    """An identity conditioner that resolves nothing (text-to-video / pre-wiring)."""

    async def resolve(self, shot: object) -> IdentityConditioning:  # noqa: ARG002
        return IdentityConditioning()


class PromptPassthroughCompiler:
    """A prompt compiler that passes the shot's prompt through unchanged."""

    def compile(
        self, shot: object, *, provider: str, model: str | None = None
    ) -> CompiledPrompt:  # noqa: ARG002
        prompt = getattr(shot, "prompt", "") or ""
        negative = getattr(shot, "negative_prompt", None)
        return CompiledPrompt(prompt=prompt, negative_prompt=negative, dialect=provider)


class PassthroughPlanner:
    """A capability planner that always renders the spec as-is (no degradation).

    Skips only when the live gate is off (the facade also enforces this before
    calling, but a planner-level skip keeps the contract honest). Reserves the
    spec's own duration as the estimate.
    """

    def plan(self, spec: WanSpec, *, budget_low: bool, live_enabled: bool) -> VideoPlan:  # noqa: ARG002
        if not live_enabled:
            return VideoPlan(
                outcome=PlanOutcome.SKIP,
                spec=spec,
                estimated_video_seconds=0.0,
                reason="live_disabled",
            )
        return VideoPlan(
            outcome=PlanOutcome.RENDER,
            spec=spec,
            estimated_video_seconds=float(spec.duration_s),
        )


# --------------------------------------------------------------------------- #
# The composition root
# --------------------------------------------------------------------------- #


def build_video_generation_service(
    *,
    router: VideoRouterProtocol,
    budget: CostBudget,
    planner: CapabilityPlanner | None = None,
    identity: IdentityConditioner | None = None,
    prompts: PromptCompiler | None = None,
    jobs: JobLifecycle | None = None,
    normalizer: OutputNormalizer | None = None,
    quality_gate: QualityGate | None = None,
    events: EventSink | None = None,
    max_attempts: int = 3,
    job_timeout_s: float | None = None,
) -> VideoGenerationService:
    """Wire a :class:`VideoGenerationService` from the composed subsystems.

    Only ``router`` and ``budget`` are required: the planner, identity
    conditioner, prompt compiler, job lifecycle and normalizer default to the
    neutral adapters above so the facade is runnable against the current
    ``app.providers`` stack. At final integration the real round-1/2
    implementations are passed for each parameter and the defaults fall away —
    the facade's flow is identical either way.
    """
    resolved_jobs = jobs or RouterJobLifecycle(router)
    return VideoGenerationService(
        planner=planner or PassthroughPlanner(),
        router=router,
        identity=identity or NullIdentityConditioner(),
        prompts=prompts or PromptPassthroughCompiler(),
        budget=budget,
        jobs=resolved_jobs,
        normalizer=normalizer or PassthroughNormalizer(),
        quality_gate=quality_gate,
        events=events,
        max_attempts=max_attempts,
        job_timeout_s=job_timeout_s,
    )


__all__ = [
    "BackendRouterAdapter",
    "NullIdentityConditioner",
    "PassthroughNormalizer",
    "PassthroughPlanner",
    "PromptPassthroughCompiler",
    "RouterJobLifecycle",
    "build_video_generation_service",
]
