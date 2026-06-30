"""Deterministic tests for ``GeneratorBridge`` — the drop-in ``VideoBackend``.

The bridge must satisfy the exact seam ``app/agents/generator.py`` calls:
``render(WanSpec) -> VideoResult``, raising ``LiveVideoDisabled`` (or a
``ProviderError``) on any non-render so the render pipeline degrades. No network.
"""

from __future__ import annotations

import pytest

from app.agents.contracts import Camera, RenderMode, ShotSpec
from app.providers.errors import LiveVideoDisabled, ProviderError, ProviderTimeout
from app.providers.types import VideoResult, WanMode, WanSpec
from app.providers.video_router import VideoBackend
from app.video.service import GeneratorBridge, build_video_generation_service
from app.video.service.protocols import (
    CostReservation,
    JobHandle,
    JobResult,
    JobTimeoutError,
)


def _video(task_id: str = "t-1") -> VideoResult:
    return VideoResult(
        duration_s=5.0,
        model="bridge-wan",
        mode=WanMode.TEXT_TO_VIDEO,
        provider_task_id=task_id,
        clip_url="https://oss/clip.mp4",
        clip_bytes=b"MP4",
        last_frame_bytes=b"PNG",
    )


class _Router:
    def __init__(self, action: object, *, name: str = "bridge-router") -> None:
        self.name = name
        self._action = action

    async def render(self, spec: WanSpec, *, budget_low: bool = False) -> VideoResult:
        if isinstance(self._action, BaseException):
            raise self._action
        assert isinstance(self._action, VideoResult)
        return self._action

    async def healthy(self) -> bool:
        return True


class _Budget:
    def __init__(self, *, live: bool = True, low: bool = False) -> None:
        self._live = live
        self._low = low
        self.committed = 0
        self.released = 0

    def can_render_live(self) -> bool:
        return self._live

    async def is_low(self) -> bool:
        return self._low

    async def reserve(self, video_seconds: float, **_: object) -> CostReservation:
        return CostReservation(id="r1", video_seconds=video_seconds)

    async def commit(
        self, reservation: CostReservation, actual_seconds: float | None = None, **_: object
    ) -> None:
        self.committed += 1

    async def release(self, reservation: CostReservation, **_: object) -> None:
        self.released += 1


def _bridge(action: object, *, live: bool = True) -> tuple[GeneratorBridge, _Budget]:
    router = _Router(action)
    budget = _Budget(live=live)
    service = build_video_generation_service(router=router, budget=budget)
    return GeneratorBridge(service, book_id="book-1"), budget


_SPEC = WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="a quiet shore", shot_id="shot-9", duration_s=5)


async def test_bridge_satisfies_video_backend_protocol() -> None:
    bridge, _budget = _bridge(_video())
    assert isinstance(bridge, VideoBackend)  # structural runtime check
    assert bridge.name == "video-service-bridge"
    assert await bridge.healthy() is True


async def test_bridge_render_unwraps_generated_clip() -> None:
    bridge, budget = _bridge(_video(task_id="task-xyz"))

    result = await bridge.render(_SPEC)

    assert isinstance(result, VideoResult)
    assert result.clip_bytes == b"MP4"
    assert result.clip_url == "https://oss/clip.mp4"
    assert result.last_frame_bytes == b"PNG"
    assert result.provider_task_id == "task-xyz"
    assert result.mode is WanMode.TEXT_TO_VIDEO
    assert budget.committed == 1


async def test_bridge_raises_live_disabled_on_gate_off() -> None:
    bridge, _budget = _bridge(_video(), live=False)
    with pytest.raises(LiveVideoDisabled):
        await bridge.render(_SPEC)


async def test_bridge_raises_provider_error_on_retryable_transport_fault() -> None:
    # A ProviderTimeout from the provider's own render is a *retryable transport*
    # fault at submit, not a JobLifecycle await-timeout. The facade retries it,
    # then (cap exhausted) skips PROVIDER_FAILED; the bridge surfaces a generic
    # ProviderError the pipeline degrades on. (The real JOB_TIMEOUT path — the
    # lifecycle's await raising JobTimeoutError → ProviderTimeout — is exercised
    # in test_video_service_facade.py::test_job_timeout_skips_releases_and_cancels.)
    bridge, _budget = _bridge(ProviderTimeout("slow"))
    with pytest.raises(ProviderError):
        await bridge.render(_SPEC)


async def test_bridge_raises_provider_error_on_hard_fault() -> None:
    bridge, _budget = _bridge(ProviderError("400 bad request", retryable=False))
    with pytest.raises(ProviderError):
        await bridge.render(_SPEC)


class _TimingOutJobs:
    """A job lifecycle whose ``await_result`` always raises ``JobTimeoutError``."""

    def __init__(self) -> None:
        self.canceled: list[str] = []

    async def submit(self, spec: WanSpec, *, budget_low: bool = False) -> JobHandle:  # noqa: ARG002
        return JobHandle(job_id="job-T", provider="bridge-router")

    async def await_result(self, handle: JobHandle, *, timeout_s: float | None = None) -> JobResult:  # noqa: ARG002
        raise JobTimeoutError("await exceeded deadline")

    async def cancel(self, handle: JobHandle) -> None:
        self.canceled.append(handle.job_id)


async def test_bridge_raises_provider_timeout_on_real_job_timeout() -> None:
    # The genuine JOB_TIMEOUT path: the lifecycle's await raises JobTimeoutError →
    # the facade SKIPs JOB_TIMEOUT → the bridge surfaces ProviderTimeout.
    budget = _Budget(live=True)
    jobs = _TimingOutJobs()
    service = build_video_generation_service(
        router=_Router(_video()), budget=budget, jobs=jobs, max_attempts=1
    )
    bridge = GeneratorBridge(service, book_id="book-1")

    with pytest.raises(ProviderTimeout):
        await bridge.render(_SPEC)
    assert jobs.canceled == ["job-T"]  # best-effort cancel fired
    assert budget.released == 1 and budget.committed == 0


async def test_bridge_render_shot_returns_rich_result() -> None:
    bridge, _budget = _bridge(_video(task_id="rs-1"))
    shot = ShotSpec(
        shot_id="shot-rich",
        render_mode=RenderMode.TEXT_TO_VIDEO,
        prompt="a campfire at dusk",
        camera=Camera(),
        seed=3,
        target_duration_s=5.0,
    )

    result = await bridge.render_shot(shot, book_id="book-1")

    assert result.generated
    assert result.shot_id == "shot-rich"
    assert result.provider_task_id == "rs-1"
    assert result.clip is not None and result.clip.clip_bytes == b"MP4"
