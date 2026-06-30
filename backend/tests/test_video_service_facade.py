"""Deterministic unit tests for the unified ``VideoGenerationService`` facade.

No network, no infra, no real video — every composed subsystem is a small
in-memory fake. The facade's contract is exercised end-to-end:

* happy path: plan → reserve → submit → await → normalize → gate → accept;
* provider failover: a router that fails the first backend then succeeds;
* degradation plan applied: the planner returns a degraded spec / a SKIP;
* budget-exceeded: ``reserve`` raises → a clean SKIP, no provider call;
* quality-gate reject → retry: a fail re-rolls the seed and retries within the cap;
* job timeout: ``await_result`` times out → a SKIP the caller degrades.

The budget ledger is asserted to never leak a reservation (release on every
non-accept, commit only on accept).
"""

from __future__ import annotations

import pytest

from app.agents.contracts import Camera, RenderMode, ShotSpec
from app.providers.errors import (
    LiveVideoDisabled,
    ProviderError,
    TransientProviderError,
)
from app.providers.types import VideoResult, WanMode, WanSpec
from app.video.service import (
    GenerationOutcome,
    SkipReason,
    VideoGenerationRequest,
    VideoGenerationService,
    build_video_generation_service,
)
from app.video.service.protocols import (
    BudgetExceededError,
    CompiledPrompt,
    CostReservation,
    IdentityConditioning,
    JobHandle,
    JobLifecycle,
    JobResult,
    JobStatus,
    JobTimeoutError,
    NormalizedClip,
    PlanOutcome,
    QualityVerdict,
    VideoPlan,
)

# --------------------------------------------------------------------------- #
# Fakes for every composed subsystem
# --------------------------------------------------------------------------- #


def _shot(shot_id: str = "shot-1", mode: RenderMode = RenderMode.TEXT_TO_VIDEO) -> ShotSpec:
    return ShotSpec(
        shot_id=shot_id,
        render_mode=mode,
        prompt="a lantern swaying in fog",
        camera=Camera(),
        seed=7,
        target_duration_s=5.0,
    )


def _request(**kw: object) -> VideoGenerationRequest:
    shot = kw.pop("shot", None) or _shot()
    return VideoGenerationRequest(shot=shot, book_id="book-1", session_id="sess-1", **kw)


def _video(model: str = "fake-wan", task_id: str = "task-1") -> VideoResult:
    return VideoResult(
        duration_s=5.0,
        model=model,
        mode=WanMode.TEXT_TO_VIDEO,
        provider_task_id=task_id,
        clip_url="https://oss/clip.mp4",
        clip_bytes=b"MP4-BYTES",
        last_frame_bytes=b"PNG-FRAME",
    )


class FakeRouter:
    """A router whose ``render`` pops scripted actions (results or exceptions)."""

    def __init__(self, script: list[object] | None = None, *, name: str = "fake-router") -> None:
        self.name = name
        self._script = list(script or [])
        self.calls: list[WanSpec] = []

    async def render(self, spec: WanSpec, *, budget_low: bool = False) -> VideoResult:
        self.calls.append(spec)
        action: object = self._script.pop(0) if self._script else _video()
        if isinstance(action, BaseException):
            raise action
        assert isinstance(action, VideoResult)
        return action

    async def healthy(self) -> bool:
        return True


class FakeBudget:
    """A budget ledger recording reserve/commit/release for leak assertions."""

    def __init__(
        self,
        *,
        live: bool = True,
        low: bool = False,
        exceed: bool = False,
        local_exceed: bool = False,
    ) -> None:
        self._live = live
        self._low = low
        self._exceed = exceed
        self._local_exceed = local_exceed
        self.reserved: list[float] = []
        self.committed: list[tuple[str, float | None]] = []
        self.released: list[str] = []
        self._n = 0

    def can_render_live(self) -> bool:
        return self._live

    async def is_low(self) -> bool:
        return self._low

    async def reserve(self, video_seconds: float, **_: object) -> CostReservation:
        if self._exceed:
            # Mimic the production BudgetExceeded by class *name* (no import).
            raise _ProductionBudgetExceeded("ceiling exhausted")
        if self._local_exceed:
            raise BudgetExceededError("ceiling exhausted")
        self._n += 1
        self.reserved.append(video_seconds)
        return CostReservation(id=f"res-{self._n}", video_seconds=video_seconds)

    async def commit(
        self, reservation: CostReservation, actual_seconds: float | None = None, **_: object
    ) -> None:
        self.committed.append((reservation.id, actual_seconds))

    async def release(self, reservation: CostReservation, **_: object) -> None:
        self.released.append(reservation.id)


class _ProductionBudgetExceeded(Exception):  # noqa: N818 — must mirror the real class name
    """A stand-in whose *class name* matches the real ``BudgetExceeded`` so the
    facade's duck-typed catch fires without importing the memory package."""


# Rename the class so ``type(exc).__name__`` is exactly "BudgetExceeded".
_ProductionBudgetExceeded.__name__ = "BudgetExceeded"
_ProductionBudgetExceeded.__qualname__ = "BudgetExceeded"


class FakePlanner:
    def __init__(self, plan: VideoPlan | None = None) -> None:
        self._plan = plan
        self.calls = 0

    def plan(self, spec: WanSpec, *, budget_low: bool, live_enabled: bool) -> VideoPlan:
        self.calls += 1
        if self._plan is not None:
            # Re-target the canned plan onto the actual spec so the loop submits it.
            return VideoPlan(
                outcome=self._plan.outcome,
                spec=self._plan.spec or spec,
                estimated_video_seconds=self._plan.estimated_video_seconds,
                provider_hint=self._plan.provider_hint,
                reason=self._plan.reason,
                degraded=self._plan.degraded,
            )
        return VideoPlan(outcome=PlanOutcome.RENDER, spec=spec, estimated_video_seconds=5.0)


class FakeIdentity:
    def __init__(self, conditioning: IdentityConditioning | None = None) -> None:
        self._c = conditioning or IdentityConditioning(identity_hash="id-abc")
        self.calls = 0

    async def resolve(self, shot: ShotSpec) -> IdentityConditioning:  # noqa: ARG002
        self.calls += 1
        return self._c


class FakePrompts:
    def __init__(self) -> None:
        self.calls = 0

    def compile(self, shot: ShotSpec, *, provider: str, model: str | None = None) -> CompiledPrompt:
        self.calls += 1
        return CompiledPrompt(
            prompt=f"[{provider}] {shot.prompt}", negative_prompt=None, dialect=provider
        )


class FakeJobs:
    """A job lifecycle scripted per-attempt: each entry is a result or an action.

    ``script`` entries:
      * ``VideoResult`` → submit ok, await succeeds with it;
      * an ``Exception`` raised from ``submit``;
      * the string ``"timeout"`` → await raises ``JobTimeoutError``;
      * a ``JobResult`` → await returns it verbatim (e.g. a non-success status).
    """

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.submitted: list[WanSpec] = []
        self.awaited: list[str] = []
        self.canceled: list[str] = []
        self._pending: dict[str, object] = {}
        self._n = 0

    async def submit(self, spec: WanSpec, *, budget_low: bool = False) -> JobHandle:
        self._n += 1
        action = self._script.pop(0) if self._script else _video()
        if isinstance(action, BaseException):
            raise action
        self.submitted.append(spec)
        job_id = f"job-{self._n}"
        self._pending[job_id] = action
        return JobHandle(job_id=job_id, provider="fake-router")

    async def await_result(self, handle: JobHandle, *, timeout_s: float | None = None) -> JobResult:
        self.awaited.append(handle.job_id)
        action = self._pending.pop(handle.job_id)
        if action == "timeout":
            raise JobTimeoutError("job timed out")
        if isinstance(action, JobResult):
            return action
        assert isinstance(action, VideoResult)
        return JobResult(status=JobStatus.SUCCEEDED, result=action)

    async def cancel(self, handle: JobHandle) -> None:
        self.canceled.append(handle.job_id)


class FakeNormalizer:
    def __init__(self) -> None:
        self.calls = 0

    async def normalize(self, result: VideoResult, *, spec: WanSpec) -> NormalizedClip:  # noqa: ARG002
        self.calls += 1
        return NormalizedClip(
            clip_bytes=result.clip_bytes,
            clip_url=result.clip_url,
            last_frame_bytes=result.last_frame_bytes,
            duration_s=float(result.duration_s),
        )


class FakeGate:
    """A quality gate that fails the first ``fail_until`` attempts, then passes."""

    def __init__(self, *, fail_until: int = 0, score: float = 0.9) -> None:
        self._fail_until = fail_until
        self._score = score
        self.calls = 0
        self.seen_seeds: list[int | None] = []

    async def evaluate(
        self, clip: NormalizedClip, *, shot: ShotSpec, spec: WanSpec, attempt: int
    ) -> QualityVerdict:  # noqa: ARG002
        self.calls += 1
        self.seen_seeds.append(spec.seed)
        if attempt <= self._fail_until:
            return QualityVerdict(passed=False, score=0.1, reason="identity_drift")
        return QualityVerdict(passed=True, score=self._score)


class RecordingEvents:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def emit(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))

    def names(self) -> list[str]:
        return [e for e, _ in self.events]


def _service(
    *,
    router: FakeRouter | None = None,
    budget: FakeBudget | None = None,
    planner: FakePlanner | None = None,
    jobs: JobLifecycle,
    normalizer: FakeNormalizer | None = None,
    gate: FakeGate | None = None,
    events: RecordingEvents | None = None,
    max_attempts: int = 3,
    job_timeout_s: float | None = 30.0,
) -> tuple[VideoGenerationService, FakeBudget, RecordingEvents]:
    budget = budget or FakeBudget()
    events = events or RecordingEvents()
    service = VideoGenerationService(
        planner=planner or FakePlanner(),
        router=router or FakeRouter(),
        identity=FakeIdentity(),
        prompts=FakePrompts(),
        budget=budget,
        jobs=jobs,
        normalizer=normalizer or FakeNormalizer(),
        quality_gate=gate,
        events=events,
        max_attempts=max_attempts,
        job_timeout_s=job_timeout_s,
    )
    return service, budget, events


# --------------------------------------------------------------------------- #
# 1. Happy path
# --------------------------------------------------------------------------- #


async def test_happy_path_generates_and_commits_actual_seconds() -> None:
    jobs = FakeJobs([_video(model="wan-x", task_id="t-9")])
    service, budget, events = _service(jobs=jobs)

    result = await service.generate(_request())

    assert result.outcome is GenerationOutcome.GENERATED
    assert result.generated
    assert result.clip is not None
    assert result.clip.clip_bytes == b"MP4-BYTES"
    assert result.model == "wan-x"
    assert result.provider == "fake-router"
    assert result.provider_task_id == "t-9"
    assert result.video_seconds == 5.0
    assert result.attempts == 1
    # Ledger: exactly one reserve, one commit at the ACTUAL seconds, no release.
    assert budget.reserved == [5.0]
    assert budget.committed == [("res-1", 5.0)]
    assert budget.released == []
    # Every orchestration step emitted, ending at accept.
    names = events.names()
    assert "video.service.plan" in names
    assert "video.service.budget_reserve" in names
    assert "video.service.submit_job" in names
    assert "video.service.await_job" in names
    assert "video.service.normalize" in names
    assert "video.service.accept" in names


async def test_compiled_prompt_and_identity_fold_into_submitted_spec() -> None:
    jobs = FakeJobs([_video()])
    service, _budget, _events = _service(jobs=jobs)

    await service.generate(_request())

    submitted = jobs.submitted[0]
    # The dialect compiler tagged the prompt; the router/jobs saw the compiled one.
    assert submitted.prompt.startswith("[fake-router] ")


# --------------------------------------------------------------------------- #
# 2. Live gate off → clean SKIP, nothing submitted, nothing reserved
# --------------------------------------------------------------------------- #


async def test_live_disabled_skips_without_touching_budget_or_provider() -> None:
    jobs = FakeJobs([_video()])
    service, budget, events = _service(budget=FakeBudget(live=False), jobs=jobs)

    result = await service.generate(_request())

    assert result.outcome is GenerationOutcome.SKIPPED
    assert result.skip_reason is SkipReason.LIVE_DISABLED
    assert result.video_seconds == 0.0
    assert budget.reserved == []  # the spend gate is sacred: no reservation
    assert jobs.submitted == []  # and no provider call
    assert "video.service.skip" in events.names()


async def test_live_disabled_mid_flight_is_a_clean_skip() -> None:
    # Plan says render (live), but submit raises LiveVideoDisabled (gate slammed).
    jobs = FakeJobs([LiveVideoDisabled("gate closed")])
    service, budget, _events = _service(jobs=jobs)

    result = await service.generate(_request())

    assert result.outcome is GenerationOutcome.SKIPPED
    assert result.skip_reason is SkipReason.LIVE_DISABLED
    # Reservation was taken then released — never committed (no leak).
    assert budget.reserved == [5.0]
    assert budget.released == ["res-1"]
    assert budget.committed == []


# --------------------------------------------------------------------------- #
# 3. Degradation plan applied
# --------------------------------------------------------------------------- #


async def test_planner_skip_degrades_without_rendering() -> None:
    plan = VideoPlan(
        outcome=PlanOutcome.SKIP,
        spec=WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt=""),
        estimated_video_seconds=0.0,
        reason="no_capable_provider",
    )
    jobs = FakeJobs([_video()])
    service, budget, _events = _service(planner=FakePlanner(plan), jobs=jobs)

    result = await service.generate(_request())

    assert result.outcome is GenerationOutcome.SKIPPED
    assert result.skip_reason is SkipReason.PLANNER_SKIP
    assert result.plan_outcome is PlanOutcome.SKIP
    assert jobs.submitted == []
    assert budget.reserved == []


async def test_degraded_plan_renders_the_degraded_spec() -> None:
    degraded_spec = WanSpec(
        mode=WanMode.TEXT_TO_VIDEO, prompt="degraded", duration_s=3, resolution="480P"
    )
    plan = VideoPlan(
        outcome=PlanOutcome.DEGRADE,
        spec=degraded_spec,
        estimated_video_seconds=3.0,
        reason="budget_low",
        degraded=True,
    )
    jobs = FakeJobs([_video()])
    service, budget, _events = _service(planner=FakePlanner(plan), jobs=jobs)

    result = await service.generate(_request())

    assert result.outcome is GenerationOutcome.GENERATED
    assert result.plan_outcome is PlanOutcome.DEGRADE
    # Reserved the degraded estimate (3s), and the submitted spec is the degraded one.
    assert budget.reserved == [3.0]
    assert jobs.submitted[0].resolution == "480P"


# --------------------------------------------------------------------------- #
# 4. Budget exceeded → SKIP, no provider call (both real-name + local mirror)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("local", [False, True])
async def test_budget_exceeded_skips_cleanly(local: bool) -> None:
    budget = FakeBudget(exceed=not local, local_exceed=local)
    jobs = FakeJobs([_video()])
    service, _budget, events = _service(budget=budget, jobs=jobs)

    result = await service.generate(_request())

    assert result.outcome is GenerationOutcome.SKIPPED
    assert result.skip_reason is SkipReason.BUDGET_EXCEEDED
    assert jobs.submitted == []  # never reached the provider
    assert budget.committed == []
    assert budget.released == []  # the reserve itself raised; nothing to release
    assert "video.service.budget_reserve" in events.names()


# --------------------------------------------------------------------------- #
# 5. Quality-gate reject → retry (seed re-rolled), then accept
# --------------------------------------------------------------------------- #


async def test_quality_reject_retries_with_new_seed_then_accepts() -> None:
    gate = FakeGate(fail_until=1)  # fail attempt 1, pass attempt 2
    jobs = FakeJobs([_video(task_id="a1"), _video(task_id="a2")])
    service, budget, _events = _service(jobs=jobs, gate=gate, max_attempts=3)

    result = await service.generate(_request())

    assert result.outcome is GenerationOutcome.GENERATED
    assert result.attempts == 2
    assert result.provider_task_id == "a2"
    # Two reserves; the rejected attempt released, the accepted one committed.
    assert budget.reserved == [5.0, 5.0]
    assert budget.released == ["res-1"]
    assert budget.committed == [("res-2", 5.0)]
    # The retry re-rolled the seed (attempt 2 != the original seed 7).
    assert gate.seen_seeds[0] == 7
    assert gate.seen_seeds[1] != 7


async def test_quality_reject_exhausts_retries_then_skips() -> None:
    gate = FakeGate(fail_until=99)  # always fail
    jobs = FakeJobs([_video(), _video()])  # 2 attempts available
    service, budget, _events = _service(jobs=jobs, gate=gate, max_attempts=2)

    result = await service.generate(_request())

    assert result.outcome is GenerationOutcome.SKIPPED
    assert result.skip_reason is SkipReason.QUALITY_REJECTED
    assert result.quality is not None and not result.quality.passed
    # Both reservations released; none committed (no spend leak on a full reject).
    assert budget.released == ["res-1", "res-2"]
    assert budget.committed == []


async def test_no_gate_ships_first_render_unconditionally() -> None:
    jobs = FakeJobs([_video()])
    service, budget, _events = _service(jobs=jobs, gate=None)

    result = await service.generate(_request())

    assert result.outcome is GenerationOutcome.GENERATED
    assert result.attempts == 1
    assert result.quality is not None and result.quality.passed  # synthetic pass
    assert budget.committed == [("res-1", 5.0)]


# --------------------------------------------------------------------------- #
# 6. Job timeout → SKIP, reservation released, cancel attempted
# --------------------------------------------------------------------------- #


async def test_job_timeout_skips_releases_and_cancels() -> None:
    jobs = FakeJobs(["timeout"])
    service, budget, events = _service(jobs=jobs, max_attempts=1)

    result = await service.generate(_request())

    assert result.outcome is GenerationOutcome.SKIPPED
    assert result.skip_reason is SkipReason.JOB_TIMEOUT
    assert budget.reserved == [5.0]
    assert budget.released == ["res-1"]
    assert budget.committed == []
    assert jobs.canceled == ["job-1"]  # best-effort cancel of the timed-out job
    assert "video.service.await_job" in events.names()


async def test_terminal_failed_status_skips_as_provider_failed() -> None:
    jobs = FakeJobs([JobResult(status=JobStatus.FAILED, error="provider 500")])
    service, budget, _events = _service(jobs=jobs, max_attempts=1)

    result = await service.generate(_request())

    assert result.outcome is GenerationOutcome.SKIPPED
    assert result.skip_reason is SkipReason.PROVIDER_FAILED
    assert budget.released == ["res-1"]
    assert budget.committed == []


# --------------------------------------------------------------------------- #
# 7. Provider failover (router-level): submit raises retryable, retry succeeds
# --------------------------------------------------------------------------- #


async def test_retryable_provider_fault_retries_then_succeeds() -> None:
    # First submit raises a retryable transport fault; second submit yields a clip.
    jobs = FakeJobs([TransientProviderError("blip"), _video(task_id="ok")])
    service, budget, _events = _service(jobs=jobs, max_attempts=3)

    result = await service.generate(_request())

    assert result.outcome is GenerationOutcome.GENERATED
    assert result.attempts == 2
    assert result.provider_task_id == "ok"
    assert budget.released == ["res-1"]  # the faulted attempt released
    assert budget.committed == [("res-2", 5.0)]


async def test_router_failover_through_adapter_is_invisible_to_facade() -> None:
    # The router fails its first backend internally, then returns a result; the
    # job lifecycle drives the router, so the facade just sees a success.
    router = FakeRouter([ProviderError("backend A down", retryable=True), _video(task_id="B")])
    from app.video.service.assembly import RouterJobLifecycle

    lifecycle = RouterJobLifecycle(router)
    service, budget, _events = _service(router=router, jobs=lifecycle, max_attempts=3)

    result = await service.generate(_request())

    # Attempt 1: RouterJobLifecycle.submit calls router.render which raises
    # (retryable) → facade releases + retries. Attempt 2: router returns B.
    assert result.outcome is GenerationOutcome.GENERATED
    assert result.provider_task_id == "B"
    assert result.attempts == 2
    assert budget.committed == [("res-2", 5.0)]


# --------------------------------------------------------------------------- #
# build_video_generation_service: defaults + neutral adapters
# --------------------------------------------------------------------------- #


async def test_assembly_runs_with_only_router_and_budget() -> None:
    router = FakeRouter([_video(model="wan-default", task_id="d-1")])
    budget = FakeBudget()
    service = build_video_generation_service(router=router, budget=budget)

    result = await service.generate(_request())

    assert result.outcome is GenerationOutcome.GENERATED
    assert result.model == "wan-default"
    assert budget.committed == [("res-1", 5.0)]


async def test_assembly_passthrough_planner_skips_when_gate_off() -> None:
    router = FakeRouter([_video()])
    budget = FakeBudget(live=False)
    service = build_video_generation_service(router=router, budget=budget)

    result = await service.generate(_request())

    assert result.outcome is GenerationOutcome.SKIPPED
    assert result.skip_reason is SkipReason.LIVE_DISABLED
    assert router.calls == []


def test_service_rejects_zero_max_attempts() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        VideoGenerationService(
            planner=FakePlanner(),
            router=FakeRouter(),
            identity=FakeIdentity(),
            prompts=FakePrompts(),
            budget=FakeBudget(),
            jobs=FakeJobs([_video()]),
            normalizer=FakeNormalizer(),
            max_attempts=0,
        )
