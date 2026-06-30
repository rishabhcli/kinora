"""Base-adapter lifecycle tests via a minimal concrete fake adapter:
the two spend gates, capability snapping, submit→poll→fetch→render, usage
accounting, timeout/failed/canceled terminal handling. No network, no keys."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.providers.errors import LiveVideoDisabled
from app.providers.types import Usage, WanMode, WanSpec
from app.video.adapters.frontier.base import BaseFrontierAdapter
from app.video.adapters.frontier.errors import (
    FrontierJobCanceled,
    FrontierJobFailed,
    FrontierTimeout,
    FrontierUnsupportedCapability,
)
from app.video.adapters.frontier.types import (
    CapabilityProfile,
    FrontierRequest,
    JobStatus,
    PollResult,
    SubmitHandle,
    VideoMode,
)

from .frontier_video_helpers import RecordingHandler, frontier_settings, make_transport


class _FakeAdapter(BaseFrontierAdapter):
    """A minimal concrete adapter: t2v+i2v, 5/10s, 720p/1080p, 16:9/9:16."""

    provider_slug = "fake"

    def _default_model(self, settings: Any) -> str:
        return "fake-1"

    def _capabilities(self) -> CapabilityProfile:
        return CapabilityProfile(
            provider="fake",
            model=self._model,
            modes=frozenset({VideoMode.TEXT_TO_VIDEO, VideoMode.IMAGE_TO_VIDEO}),
            durations_s=(5.0, 10.0),
            resolutions=("720p", "1080p"),
            aspect_ratios=("16:9", "9:16"),
            fps_options=(),
            supports_seed=True,
            supports_negative_prompt=True,
            max_reference_images=1,
            max_prompt_chars=0,
        )

    def _build_submit(self, request: FrontierRequest) -> tuple[str, dict[str, Any]]:
        return "submit", {"prompt": request.prompt, "duration": request.duration_s}

    def _parse_submit(self, body: dict[str, Any]) -> str:
        return str(body.get("id") or "")

    def _build_poll(self, handle: SubmitHandle) -> tuple[str, str, dict[str, Any] | None]:
        return "GET", f"poll/{handle.job_id}", None

    def _parse_poll(self, body: dict[str, Any]) -> PollResult:
        status = {
            "done": JobStatus.SUCCEEDED,
            "fail": JobStatus.FAILED,
            "cancel": JobStatus.CANCELED,
        }.get(str(body.get("state")), JobStatus.PENDING)
        if status is JobStatus.SUCCEEDED:
            return PollResult(
                status=status,
                asset_url=body.get("url"),
                last_frame_url=body.get("last_frame"),
                duration_s=body.get("duration"),
            )
        return PollResult(status=status, detail=body.get("detail"))


def _adapter(
    handler: Any,
    *,
    live: bool = True,
    enabled: bool = True,
    usage_recorder: Any = None,
) -> _FakeAdapter:
    settings = frontier_settings(live=live, enabled=enabled)
    tx = make_transport(handler, provider="fake", enabled=enabled)
    return _FakeAdapter(
        settings,
        tx,
        usage_recorder=usage_recorder,
        poll_interval_s=0.0,
        poll_max_interval_s=0.0,
        poll_timeout_s=5.0,
    )


# --------------------------------------------------------------------------- #
# Gating
# --------------------------------------------------------------------------- #


async def test_render_raises_live_disabled_without_network() -> None:
    def tripwire(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("no network when the live gate is off")

    adapter = _adapter(tripwire, live=False)
    with pytest.raises(LiveVideoDisabled):
        await adapter.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="meadow"))


async def test_submit_raises_live_disabled_too() -> None:
    adapter = _adapter(lambda r: httpx.Response(200, json={"id": "x"}), live=False)
    with pytest.raises(LiveVideoDisabled):
        await adapter.submit(FrontierRequest(mode=VideoMode.TEXT_TO_VIDEO, prompt="x"))


async def test_healthy_is_true_no_network() -> None:
    def tripwire(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("healthy must not touch the network")

    adapter = _adapter(tripwire, live=False, enabled=False)
    assert await adapter.healthy() is True
    assert adapter.name == "fake:fake-1"
    assert adapter.capabilities().provider == "fake"


# --------------------------------------------------------------------------- #
# Capability prepare/snap/validate
# --------------------------------------------------------------------------- #


def test_prepare_snaps_duration_and_default_resolution() -> None:
    adapter = _adapter(lambda r: httpx.Response(200, json={}))
    req = FrontierRequest(
        mode=VideoMode.TEXT_TO_VIDEO, prompt="x", duration_s=7.0, resolution="720p"
    )
    prepared = adapter.prepare(req)
    assert prepared.duration_s == 5.0  # snapped (tie → shorter)
    assert prepared.resolution == "720p"  # supported → unchanged


def test_prepare_rejects_explicit_unsupported_resolution() -> None:
    adapter = _adapter(lambda r: httpx.Response(200, json={}))
    req = FrontierRequest(
        mode=VideoMode.TEXT_TO_VIDEO, prompt="x", resolution="4k", aspect_ratio="16:9"
    )
    with pytest.raises(FrontierUnsupportedCapability):
        adapter.prepare(req)


# --------------------------------------------------------------------------- #
# Lifecycle: submit → poll → fetch → render
# --------------------------------------------------------------------------- #


async def test_render_full_success_path_records_usage_and_downloads() -> None:
    recorded: list[Usage] = []
    handler = RecordingHandler(
        {
            "submit": httpx.Response(200, json={"id": "job-9"}),
            "poll/job-9": httpx.Response(
                200,
                json={
                    "state": "done",
                    "url": "https://cdn.test/clip.mp4",
                    "last_frame": "https://cdn.test/last.png",
                    "duration": 5.0,
                },
            ),
            "clip.mp4": httpx.Response(200, content=b"CLIP"),
            "last.png": httpx.Response(200, content=b"LAST"),
        }
    )
    adapter = _adapter(handler, usage_recorder=recorded.append)
    result = await adapter.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="meadow"))
    assert result.clip_bytes == b"CLIP"
    assert result.last_frame_bytes == b"LAST"
    assert result.clip_url == "https://cdn.test/clip.mp4"
    assert result.provider_task_id == "job-9"
    assert result.duration_s == 5.0
    assert result.model == "fake-1"
    assert result.mode is WanMode.TEXT_TO_VIDEO
    assert len(recorded) == 1
    assert recorded[0].video_seconds == 5.0
    assert recorded[0].operation == "video"


async def test_poll_loop_waits_through_pending_then_succeeds() -> None:
    states = iter(["queued", "queued", "done"])

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("submit"):
            return httpx.Response(200, json={"id": "j"})
        if "/poll/" in path:
            state = next(states)
            if state == "done":
                return httpx.Response(200, json={"state": "done", "url": "u", "duration": 5})
            return httpx.Response(200, json={"state": state})
        if path.endswith("/u") or path == "/u":
            return httpx.Response(200, content=b"OK")
        return httpx.Response(200, content=b"OK")

    adapter = _adapter(handler)
    result = await adapter.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x"))
    assert result.clip_bytes == b"OK"


async def test_render_failed_job_raises_job_failed() -> None:
    handler = RecordingHandler(
        {
            "submit": httpx.Response(200, json={"id": "j"}),
            "poll/j": httpx.Response(200, json={"state": "fail", "detail": "nsfw"}),
        }
    )
    adapter = _adapter(handler)
    with pytest.raises(FrontierJobFailed) as ei:
        await adapter.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x"))
    assert "nsfw" in str(ei.value)
    assert ei.value.retryable is False


async def test_render_canceled_job_raises_job_canceled() -> None:
    handler = RecordingHandler(
        {
            "submit": httpx.Response(200, json={"id": "j"}),
            "poll/j": httpx.Response(200, json={"state": "cancel"}),
        }
    )
    adapter = _adapter(handler)
    with pytest.raises(FrontierJobCanceled):
        await adapter.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x"))


async def test_poll_to_completion_times_out() -> None:
    handler = RecordingHandler(
        {
            "submit": httpx.Response(200, json={"id": "j"}),
            "poll/j": httpx.Response(200, json={"state": "queued"}),
        }
    )
    settings = frontier_settings()
    tx = make_transport(handler, provider="fake")
    adapter = _FakeAdapter(
        settings, tx, poll_interval_s=0.0, poll_max_interval_s=0.0, poll_timeout_s=0.0
    )
    handle = SubmitHandle(provider="fake", model="fake-1", job_id="j")
    with pytest.raises(FrontierTimeout):
        await adapter.poll_to_completion(handle)


async def test_fetch_without_succeeded_raises() -> None:
    adapter = _adapter(lambda r: httpx.Response(200, json={}))
    with pytest.raises(Exception):  # noqa: B017 - FrontierError base
        await adapter.fetch(PollResult(status=JobStatus.PENDING))


async def test_render_duration_falls_back_to_snapped_when_poll_omits_it() -> None:
    handler = RecordingHandler(
        {
            "submit": httpx.Response(200, json={"id": "j"}),
            # no duration in the poll body
            "poll/j": httpx.Response(200, json={"state": "done", "url": "https://c/clip.mp4"}),
            "clip.mp4": httpx.Response(200, content=b"C"),
        }
    )
    adapter = _adapter(handler)
    # request 9s → snapped to 10s; poll omits duration → result uses snapped 10s.
    result = await adapter.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=9))
    assert result.duration_s == 10.0
