"""Unit tests for the UniversalVideoProvider contract + the EchoVideoProvider
reference fake: protocol conformance, the submit->poll->fetch lifecycle, the
synchronous fast-path, deterministic clips, cancel, failure paths, and the
BaseVideoProvider.render loop (with a deterministic, no-real-sleep clock).
"""

from __future__ import annotations

import asyncio

import pytest

from app.video.abstraction.capability import SubmitStyle, VideoMode
from app.video.abstraction.echo import EchoVideoProvider, default_echo_capability
from app.video.abstraction.provider import (
    BaseVideoProvider,
    UniversalVideoProvider,
    VideoProviderError,
    VideoRenderTimeout,
)
from app.video.abstraction.schema import (
    CanonicalVideoRequest,
    MediaRef,
    MediaRole,
    TaskState,
    VideoTaskHandle,
)


def _req(**kw: object) -> CanonicalVideoRequest:
    base: dict[str, object] = {
        "mode": VideoMode.TEXT_TO_VIDEO,
        "prompt": "vista",
        "duration_s": 5.0,
    }
    base.update(kw)
    return CanonicalVideoRequest(**base)  # type: ignore[arg-type]


# -- protocol conformance ------------------------------------------------- #


def test_echo_is_a_universal_video_provider() -> None:
    assert isinstance(EchoVideoProvider(), UniversalVideoProvider)


def test_provider_id_matches_capability() -> None:
    p = EchoVideoProvider(default_echo_capability("echo-1"))
    assert p.provider_id == "echo-1" == p.capabilities().provider_id


# -- async lifecycle ------------------------------------------------------ #


async def test_submit_poll_fetch_async() -> None:
    p = EchoVideoProvider(poll_steps=2)
    handle = await p.submit(_req())
    assert handle.state is TaskState.RUNNING
    handle = await p.poll(handle)
    assert handle.state is TaskState.RUNNING  # 1 of 2 polls
    handle = await p.poll(handle)
    assert handle.state is TaskState.SUCCEEDED
    result = await p.fetch(handle)
    assert result.clip_bytes is not None
    assert result.clip_url == f"echo://{handle.task_id}.mp4"
    assert result.provider_id == "echo"


async def test_submit_poll_zero_steps_is_immediate() -> None:
    p = EchoVideoProvider(poll_steps=0)
    handle = await p.submit(_req())
    assert handle.state is TaskState.SUCCEEDED
    assert handle.inline_result is not None


async def test_synchronous_provider_returns_inline() -> None:
    cap = default_echo_capability("sync").model_copy(
        update={"submit_style": SubmitStyle.SYNCHRONOUS}
    )
    p = EchoVideoProvider(cap, poll_steps=5)  # poll_steps ignored when sync
    handle = await p.submit(_req())
    assert handle.state is TaskState.SUCCEEDED
    assert handle.inline_result is not None


# -- determinism ---------------------------------------------------------- #


async def test_identical_requests_are_byte_identical() -> None:
    p = EchoVideoProvider(poll_steps=0)
    r1 = await p.fetch(await p.submit(_req(seed=7)))
    r2 = await p.fetch(await p.submit(_req(seed=7)))
    assert r1.clip_bytes == r2.clip_bytes
    assert r1.last_frame_bytes == r2.last_frame_bytes
    assert r1.provider_task_id == r2.provider_task_id


async def test_different_requests_differ() -> None:
    p = EchoVideoProvider(poll_steps=0)
    a = await p.fetch(await p.submit(_req(seed=7)))
    b = await p.fetch(await p.submit(_req(seed=8)))
    assert a.clip_bytes != b.clip_bytes


async def test_result_geometry_defaults_from_capability() -> None:
    p = EchoVideoProvider(poll_steps=0)
    result = await p.fetch(await p.submit(_req()))  # no resolution/fps in request
    cap = p.capabilities()
    assert result.resolution == cap.default_resolution
    assert result.fps == cap.default_fps


async def test_result_snaps_duration_to_window() -> None:
    cap = default_echo_capability("e").model_copy(
        update={"min_duration_s": 2.0, "max_duration_s": 6.0, "discrete_durations_s": ()}
    )
    p = EchoVideoProvider(cap, poll_steps=0)
    result = await p.fetch(await p.submit(_req(duration_s=6.0)))
    assert result.duration_s == 6.0


# -- validation ----------------------------------------------------------- #


async def test_submit_rejects_unsupported_mode() -> None:
    cap = default_echo_capability("t2v").model_copy(
        update={"modes": frozenset({VideoMode.TEXT_TO_VIDEO})}
    )
    p = EchoVideoProvider(cap)
    with pytest.raises(VideoProviderError, match="does not support mode"):
        await p.submit(_req(mode=VideoMode.REFERENCE_TO_VIDEO))


async def test_submit_rejects_out_of_window_duration() -> None:
    cap = default_echo_capability("e").model_copy(
        update={"min_duration_s": 2.0, "max_duration_s": 5.0, "discrete_durations_s": ()}
    )
    p = EchoVideoProvider(cap)
    with pytest.raises(VideoProviderError, match="cannot render"):
        await p.submit(_req(duration_s=99.0))


# -- failure + cancel ----------------------------------------------------- #


async def test_failed_task_cannot_be_fetched() -> None:
    req = _req(shot_id="doomed")
    p = EchoVideoProvider(poll_steps=0, fail_keys=frozenset({"doomed"}))
    handle = await p.submit(req)
    assert handle.state is TaskState.FAILED
    with pytest.raises(VideoProviderError, match="cannot fetch"):
        await p.fetch(handle)


async def test_cancel_marks_canceled() -> None:
    p = EchoVideoProvider(poll_steps=5)
    handle = await p.submit(_req())
    canceled = await p.cancel(handle)
    assert canceled.state is TaskState.CANCELED
    # a subsequent poll keeps it terminal-canceled
    assert (await p.poll(canceled)).state is TaskState.CANCELED


async def test_cancel_unsupported_raises() -> None:
    cap = default_echo_capability("nocancel").model_copy(update={"supports_cancel": False})
    p = EchoVideoProvider(cap, poll_steps=5)
    handle = await p.submit(_req())
    with pytest.raises(VideoProviderError, match="does not support cancel"):
        await p.cancel(handle)


async def test_poll_unknown_task_raises() -> None:
    p = EchoVideoProvider()
    stray = VideoTaskHandle(provider_id="echo", task_id="echo-deadbeef", state=TaskState.RUNNING)
    with pytest.raises(VideoProviderError, match="unknown echo task"):
        await p.poll(stray)


async def test_call_counters() -> None:
    p = EchoVideoProvider(poll_steps=1)
    h = await p.submit(_req())
    h = await p.poll(h)
    await p.fetch(h)
    assert (p.submit_calls, p.poll_calls, p.fetch_calls) == (1, 1, 1)


# -- BaseVideoProvider.render loop --------------------------------------- #


async def test_render_loop_drives_to_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    # patch sleep so the poll loop doesn't actually wait
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    p = EchoVideoProvider(poll_steps=3)
    result = await p.render(_req(), interval_s=0.01, max_interval_s=0.01, timeout_s=5.0)
    assert result.clip_bytes is not None
    assert p.poll_calls >= 1


async def test_render_loop_inline_skips_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    p = EchoVideoProvider(poll_steps=0)  # terminal at submit
    result = await p.render(_req())
    assert result.clip_bytes is not None
    assert p.poll_calls == 0  # never polled — inline result used


async def test_render_loop_raises_on_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    p = EchoVideoProvider(poll_steps=0, fail_keys=frozenset({"x"}))
    with pytest.raises(VideoProviderError, match="ended failed"):
        await p.render(_req(shot_id="x"))


async def test_render_loop_times_out() -> None:
    """A task that never terminates trips VideoRenderTimeout via a fake clock."""

    class _StuckProvider(BaseVideoProvider):
        provider_id = "stuck"

        def __init__(self) -> None:
            self._t = 0.0

        def capabilities(self):  # type: ignore[override]
            return default_echo_capability("stuck")

        async def submit(self, request):  # type: ignore[override]
            return VideoTaskHandle(provider_id="stuck", task_id="t", state=TaskState.RUNNING)

        async def poll(self, handle):  # type: ignore[override]
            return handle  # never advances

        async def fetch(self, handle):  # pragma: no cover - never reached
            raise AssertionError

        async def cancel(self, handle):  # type: ignore[override]
            return handle.model_copy(update={"state": TaskState.CANCELED})

    p = _StuckProvider()

    # Drive a fake monotonic clock so the deadline is hit without real waiting.
    loop = asyncio.get_event_loop()
    real_time = loop.time
    ticks = iter([0.0, 0.5, 1.0, 2.0, 10.0, 100.0])

    def _fake_time() -> float:
        try:
            return next(ticks)
        except StopIteration:
            return 1000.0

    loop.time = _fake_time  # type: ignore[method-assign]
    try:
        with pytest.raises(VideoRenderTimeout):
            await p.render(_req(), timeout_s=1.0, interval_s=0.0, max_interval_s=0.0)
    finally:
        loop.time = real_time  # type: ignore[method-assign]


# -- reference media flows through the lifecycle -------------------------- #


async def test_r2v_request_round_trips_through_echo() -> None:
    req = _req(
        mode=VideoMode.REFERENCE_TO_VIDEO,
        media=(
            MediaRef(role=MediaRole.REFERENCE, url="r1"),
            MediaRef(role=MediaRole.REFERENCE_VOICE, url="v"),
        ),
    )
    p = EchoVideoProvider(poll_steps=0)
    result = await p.fetch(await p.submit(req))
    assert result.mode is VideoMode.REFERENCE_TO_VIDEO
    assert result.clip_bytes is not None
