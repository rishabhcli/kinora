"""Local structural protocols the :class:`VideoGenerationService` composes.

The keystone facade orchestrates **eight** earlier-phase subsystems — the
universal provider abstraction, the multi-provider router, the capability
planner/degradation policy, cross-provider cost/budget, identity conditioning,
the async job lifecycle, prompt-dialect compilation, and output normalization.

In this FINAL round those packages are *not* importable into this worktree, so
every dependency the facade needs is declared here as a minimal **structural**
``Protocol`` (PEP 544) mirroring the shape the real round-1/2 implementations
already expose. The orchestrator wires the concrete classes at final integration
— because nothing here is a nominal base class, an unmodified real implementation
satisfies the protocol the moment its method signatures line up.

Design rules honoured:

* **No imports from sibling round-1/2 packages.** Only ``app.providers.types`` and
  ``app.agents.contracts`` (the stable, already-merged contract surface) are
  referenced — these are the shapes the render pipeline already speaks.
* **Async-first.** Every I/O-shaped seam (router render, job submit/await,
  download, identity resolve) is ``async``; pure-policy seams (planner, cost,
  dialect compile) are synchronous, matching the real subsystems.
* **Runtime-checkable where it helps tests.** The seams a fake most often stands
  in for are ``@runtime_checkable`` so a test double can be asserted structurally.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.agents.contracts import ShotSpec
from app.providers.types import VideoResult, WanSpec

# --------------------------------------------------------------------------- #
# 1. Universal provider abstraction + 2. multi-provider router
# --------------------------------------------------------------------------- #


@runtime_checkable
class VideoRouterProtocol(Protocol):
    """The composed router/abstraction seam (round-1 ``app.video.routing``).

    Mirrors :class:`app.providers.video_router.VideoRouter` /
    :class:`~app.providers.video_router.VideoBackend`: a single ``render`` that
    fails over (or hedges) across the universal-abstraction backends underneath.
    The optional ``budget_low`` keyword matches the real router's cost-aware mode;
    the facade always passes it explicitly so a plain ``VideoBackend`` (which does
    not accept it) is *also* adaptable via
    :class:`app.video.service.assembly.BackendRouterAdapter`.
    """

    name: str

    async def render(self, spec: WanSpec, *, budget_low: bool = False) -> VideoResult:
        """Render ``spec`` to a clip, failing over across backends."""
        ...

    async def healthy(self) -> bool:
        """``True`` when at least one backend is routable (no render spend)."""
        ...


# --------------------------------------------------------------------------- #
# 3. capability planner / degradation policy
# --------------------------------------------------------------------------- #


class PlanOutcome(StrEnum):
    """What the capability planner decided for a shot (round-1 ``app.video.planning``)."""

    #: Render at full quality on the selected provider/model.
    RENDER = "render"
    #: Render, but with a degraded spec (lower resolution / shorter / cheaper model).
    DEGRADE = "degrade"
    #: Do not render — hand the shot to the ffmpeg Ken-Burns lane (off-gate / no capable provider).
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class VideoPlan:
    """The planner's verdict for one shot.

    Attributes:
        outcome: render / degrade / skip.
        spec: the (possibly degraded) :class:`WanSpec` to submit when rendering.
        estimated_video_seconds: seconds to reserve against the budget.
        provider_hint: an optional preferred backend name (the router may still
            fail over away from it).
        reason: a short machine label explaining a degrade/skip (telemetry).
        degraded: convenience flag — ``True`` iff ``outcome is DEGRADE``.
    """

    outcome: PlanOutcome
    spec: WanSpec
    estimated_video_seconds: float
    provider_hint: str | None = None
    reason: str | None = None
    degraded: bool = False


class CapabilityPlanner(Protocol):
    """Pure capability/degradation policy (round-1 ``app.video.planning``).

    Given the resolved :class:`WanSpec` and whether the budget is low, decide
    whether to render at full quality, render a degraded spec, or skip to the
    ffmpeg lane. Deterministic — no I/O, no RNG.
    """

    def plan(self, spec: WanSpec, *, budget_low: bool, live_enabled: bool) -> VideoPlan:
        """Return the :class:`VideoPlan` for ``spec``."""
        ...


# --------------------------------------------------------------------------- #
# 4. cross-provider cost / budget
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CostReservation:
    """An opaque handle to a held budget reservation (round-2 ``app.video.cost``).

    Mirrors :class:`app.memory.budget_service.Reservation` structurally: the
    facade only needs an id to commit/release, plus the reserved amount for
    telemetry. The real budget service's ``Reservation`` satisfies this by having
    an ``id`` attribute.
    """

    id: str
    video_seconds: float


class CostBudget(Protocol):
    """The cross-provider cost/budget seam (round-2 ``app.video.cost``).

    Wraps the §11 video-seconds ledger: the go-live gate, a "running low" probe,
    and reserve/commit/release. The facade reserves *before* submitting a job and
    commits the **actual** rendered seconds (or releases on any failure) so the
    ledger never leaks a reservation.
    """

    def can_render_live(self) -> bool:
        """The §11 go-live gate (also reflects ``KINORA_LIVE_VIDEO``)."""
        ...

    async def is_low(self) -> bool:
        """``True`` when remaining video-seconds are below the floor."""
        ...

    async def reserve(
        self,
        video_seconds: float,
        *,
        session_id: str | None = None,
        scene_id: str | None = None,
        book_id: str | None = None,
        note: str | None = None,
    ) -> CostReservation:
        """Hold ``video_seconds`` against the ceiling; raises on exceed."""
        ...

    async def commit(
        self,
        reservation: CostReservation,
        actual_seconds: float | None = None,
        *,
        note: str | None = None,
    ) -> None:
        """Settle a reservation at the actually-rendered seconds."""
        ...

    async def release(self, reservation: CostReservation, *, note: str | None = None) -> None:
        """Release a held reservation that never rendered (no spend)."""
        ...


class BudgetExceededError(Exception):
    """Raised by :meth:`CostBudget.reserve` when the ceiling is exhausted.

    A *local* mirror of ``app.memory.budget_service.BudgetExceeded`` so the
    facade's tests need no infra; the real ``reserve`` raising its own
    ``BudgetExceeded`` is caught by the facade via duck-typed name matching (see
    :func:`app.video.service.service._is_budget_exceeded`).
    """


# --------------------------------------------------------------------------- #
# 5. identity conditioning
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IdentityConditioning:
    """Resolved identity inputs for a shot (round-2 ``app.video.identity``).

    Attributes:
        reference_image_urls: locked character/appearance reference URLs to fold
            into the ``WanSpec`` (r2v) — already persisted + signed.
        image_url: the single driving/start-frame URL (i2v / continuation).
        first_frame_url / last_frame_url: endpoint frames (first-last-frame mode).
        source_video_url: prior accepted clip URL (continuation / instruction-edit).
        reference_voice_url: cloned-voice reference (r2v voice conditioning).
        identity_hash: a stable digest of the locked reference set (cache key part).
    """

    reference_image_urls: Sequence[str] = ()
    image_url: str | None = None
    first_frame_url: str | None = None
    last_frame_url: str | None = None
    source_video_url: str | None = None
    reference_voice_url: str | None = None
    identity_hash: str | None = None


class IdentityConditioner(Protocol):
    """The identity-conditioning seam (round-2 ``app.video.identity``).

    Resolves a shot's locked canon references (characters/props/style + the prior
    endpoint frame) into signed URLs ready to attach to a :class:`WanSpec`. Async
    because it persists/signs object-storage references.
    """

    async def resolve(self, shot: ShotSpec) -> IdentityConditioning:
        """Resolve identity conditioning inputs for ``shot``."""
        ...


# --------------------------------------------------------------------------- #
# 6. prompt-dialect compilation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CompiledPrompt:
    """A provider-dialect-compiled prompt (round-1 ``app.video.prompts``).

    Attributes:
        prompt: the positive prompt in the target provider's dialect.
        negative_prompt: the negative prompt (or ``None`` if the dialect has none).
        dialect: the dialect id this was compiled for (telemetry).
    """

    prompt: str
    negative_prompt: str | None = None
    dialect: str = "default"


class PromptCompiler(Protocol):
    """The prompt-dialect compiler seam (round-1 ``app.video.prompts.dialects``).

    Translates the shot's source prompt + camera grammar into the dialect the
    *selected* provider/model expects (Wan vs MiniMax vs …). Pure/synchronous.
    """

    def compile(self, shot: ShotSpec, *, provider: str, model: str | None = None) -> CompiledPrompt:
        """Compile ``shot``'s prompt for the named provider/model."""
        ...


# --------------------------------------------------------------------------- #
# 7. async job lifecycle
# --------------------------------------------------------------------------- #


class JobStatus(StrEnum):
    """Terminal/active states of an async render job (round-2 ``app.video.jobs``)."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class JobHandle:
    """A handle to a submitted async render job (round-2 ``app.video.jobs``)."""

    job_id: str
    provider: str


@dataclass(frozen=True, slots=True)
class JobResult:
    """The terminal outcome of an awaited job.

    Carries the provider :class:`VideoResult` on success; on a non-success status
    ``result`` is ``None`` and ``error`` explains the terminal state.
    """

    status: JobStatus
    result: VideoResult | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is JobStatus.SUCCEEDED and self.result is not None


class JobTimeoutError(Exception):
    """Raised when an awaited job exceeds its deadline (local mirror of the
    round-2 jobs timeout)."""


class JobLifecycle(Protocol):
    """The async job-lifecycle seam (round-2 ``app.video.jobs``).

    Wraps "submit a render task, await it to a terminal state": for hosted Wan
    that is DashScope's submit→poll→download flow. ``submit`` routes through the
    composed router (so failover/hedging still apply); ``await_result`` polls to a
    terminal :class:`JobResult` (or raises :class:`JobTimeoutError`).
    """

    async def submit(self, spec: WanSpec, *, budget_low: bool = False) -> JobHandle:
        """Submit a render task; returns a :class:`JobHandle`."""
        ...

    async def await_result(self, handle: JobHandle, *, timeout_s: float | None = None) -> JobResult:
        """Poll ``handle`` to a terminal :class:`JobResult` (or time out)."""
        ...

    async def cancel(self, handle: JobHandle) -> None:
        """Best-effort cancel of a still-running job."""
        ...


# --------------------------------------------------------------------------- #
# 8. output normalization
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class NormalizedClip:
    """A normalized, persisted clip (round-2 ``app.video.normalize``).

    Attributes:
        clip_bytes: the downloaded, container/codec-normalized mp4 bytes (or
            ``None`` when only a persisted URL is available).
        clip_url: a signed URL to the persisted clip.
        last_frame_bytes: the extracted final frame (the continuation anchor).
        duration_s: the measured clip duration in seconds.
        width / height: normalized pixel dimensions, when probed.
    """

    clip_bytes: bytes | None = None
    clip_url: str | None = None
    last_frame_bytes: bytes | None = None
    duration_s: float = 0.0
    width: int | None = None
    height: int | None = None


class OutputNormalizer(Protocol):
    """The output-normalization seam (round-2 ``app.video.normalize``).

    Takes the provider's raw :class:`VideoResult` (a possibly-expiring task URL +
    maybe bytes) and returns a :class:`NormalizedClip`: bytes downloaded if
    needed, container/codec normalized, last frame extracted, duration probed.
    """

    async def normalize(self, result: VideoResult, *, spec: WanSpec) -> NormalizedClip:
        """Normalize a provider result into a persisted, probed clip."""
        ...


# --------------------------------------------------------------------------- #
# Quality gate (the facade's own §9.5-style retry trigger)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class QualityVerdict:
    """A quality-gate decision over a normalized clip.

    Attributes:
        passed: ``True`` to accept; ``False`` triggers a bounded retry.
        score: a 0..1 quality score (telemetry / threshold tuning).
        reason: a short label for a reject (e.g. ``"identity_drift"``).
    """

    passed: bool
    score: float = 1.0
    reason: str | None = None


class QualityGate(Protocol):
    """The facade's quality gate (a Critic-shaped seam, round-1 capability).

    Scores a normalized clip against the shot's intent; a fail triggers a bounded
    retry (re-roll seed / re-condition) within the facade before falling through
    to a SKIP. Optional — when no gate is injected the facade ships the first
    successful render unconditionally (matching the pipeline's advisory-QA stance).
    """

    async def evaluate(
        self, clip: NormalizedClip, *, shot: ShotSpec, spec: WanSpec, attempt: int
    ) -> QualityVerdict:
        """Score ``clip`` for ``shot``; ``passed=False`` triggers a retry."""
        ...


# --------------------------------------------------------------------------- #
# Structured event sink
# --------------------------------------------------------------------------- #


@runtime_checkable
class EventSink(Protocol):
    """A sink for the facade's structured per-step events.

    The facade emits one event per orchestration step (plan → select → condition →
    compile → submit → await → normalize → gate → accept/skip). The default sink
    logs via structlog; a test sink records them for assertions; the live feed
    sink (round-2) fans them to the director UI.
    """

    def emit(self, event: str, **fields: object) -> None:
        """Record one structured event (counts/ids only — never prompt content)."""
        ...


# A read-only view of provider cost tiers, for cost-aware planning telemetry.
CostTierMap = Mapping[str, float]


__all__ = [
    "BudgetExceededError",
    "CapabilityPlanner",
    "CompiledPrompt",
    "CostBudget",
    "CostReservation",
    "CostTierMap",
    "EventSink",
    "IdentityConditioner",
    "IdentityConditioning",
    "JobHandle",
    "JobLifecycle",
    "JobResult",
    "JobStatus",
    "JobTimeoutError",
    "NormalizedClip",
    "OutputNormalizer",
    "PlanOutcome",
    "PromptCompiler",
    "QualityGate",
    "QualityVerdict",
    "VideoPlan",
    "VideoRouterProtocol",
]
