"""Unit tests for the Wan video provider: the LiveVideoDisabled spend gate,
mode→model/param mapping, cheap (no-render) model verification, and a mocked
submit→poll→download success path."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.providers.base import ProviderClient, ResilienceConfig
from app.providers.errors import LiveVideoDisabled
from app.providers.types import WanMode, WanSpec
from app.providers.video import VideoPollConfig, VideoProvider
from tests.test_providers_base import FAST


def _settings(*, live: bool) -> Settings:
    return Settings(dashscope_api_key="test", kinora_live_video=live)


def _client(handler: object, *, live: bool) -> ProviderClient:
    return ProviderClient(
        _settings(live=live),
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        resilience=FAST,
    )


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={})


# --------------------------------------------------------------------------- #
# THE GATE
# --------------------------------------------------------------------------- #


async def test_render_raises_when_live_video_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import dashscope

    submitted = {"called": False}

    def _tripwire(**kwargs: object) -> object:
        submitted["called"] = True
        raise AssertionError("async_call must NOT be invoked when the gate is closed")

    monkeypatch.setattr(dashscope.VideoSynthesis, "async_call", _tripwire)

    client = _client(_ok, live=False)
    provider = VideoProvider(client)
    with pytest.raises(LiveVideoDisabled):
        await provider.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="a quiet meadow"))
    assert submitted["called"] is False  # no Wan task was ever submitted
    await client.aclose()


# --------------------------------------------------------------------------- #
# Mode → model + request shape
# --------------------------------------------------------------------------- #


def test_model_for_mode_resolution() -> None:
    client = _client(_ok, live=True)
    provider = VideoProvider(client)
    s = client.settings
    assert provider._model_for(WanSpec(mode=WanMode.TEXT_TO_VIDEO)) == s.video_model
    assert provider._model_for(WanSpec(mode=WanMode.IMAGE_TO_VIDEO)) == s.video_model_i2v
    assert provider._model_for(WanSpec(mode=WanMode.REFERENCE_TO_VIDEO)) == s.video_model_r2v
    assert provider._model_for(WanSpec(mode=WanMode.FIRST_LAST_FRAME)) == s.video_model_i2v
    assert provider._model_for(WanSpec(mode=WanMode.VIDEO_CONTINUATION)) == s.video_model_i2v
    assert provider._model_for(WanSpec(mode=WanMode.INSTRUCTION_EDIT)) == s.video_model_i2v
    assert (
        provider._model_for(WanSpec(mode=WanMode.TEXT_TO_VIDEO, model="custom-wan")) == "custom-wan"
    )


def test_submit_kwargs_per_mode() -> None:
    client = _client(_ok, live=True)
    provider = VideoProvider(client)

    t2v = provider._submit_kwargs(
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="p", negative_prompt="bad", seed=3), "wan2.7-t2v"
    )
    assert t2v["prompt"] == "p" and t2v["negative_prompt"] == "bad" and t2v["seed"] == 3
    assert "img_url" not in t2v and "media" not in t2v

    i2v = provider._submit_kwargs(
        WanSpec(mode=WanMode.IMAGE_TO_VIDEO, image_url="https://x/first.png"), "wan2.7-i2v"
    )
    assert i2v["img_url"] == "https://x/first.png"

    flf = provider._submit_kwargs(
        WanSpec(
            mode=WanMode.FIRST_LAST_FRAME,
            first_frame_url="https://x/a.png",
            last_frame_url="https://x/b.png",
        ),
        "wan2.7-i2v",
    )
    assert flf["first_frame_url"] == "https://x/a.png"
    assert flf["last_frame_url"] == "https://x/b.png"

    r2v = provider._submit_kwargs(
        WanSpec(
            mode=WanMode.REFERENCE_TO_VIDEO,
            reference_image_urls=["https://x/ref1.png", "https://x/ref2.png"],
            reference_voice_url="https://x/voice.mp3",
        ),
        "wan2.7-r2v",
    )
    assert r2v["media"][0] == {
        "type": "reference_image",
        "url": "https://x/ref1.png",
        "reference_voice": "https://x/voice.mp3",
    }
    assert r2v["media"][1] == {"type": "reference_image", "url": "https://x/ref2.png"}

    edit = provider._submit_kwargs(
        WanSpec(
            mode=WanMode.INSTRUCTION_EDIT,
            prompt="make it red",
            source_video_url="https://x/clip.mp4",
        ),
        "wan2.7-i2v",
    )
    assert edit["media"] == [{"type": "video", "url": "https://x/clip.mp4"}]


# --------------------------------------------------------------------------- #
# verify_model_available (NO render)
# --------------------------------------------------------------------------- #


async def test_verify_model_available_true_and_cancels() -> None:
    state = {"submitted": False, "cancelled": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/video-synthesis"):
            state["submitted"] = True
            return httpx.Response(
                200, json={"output": {"task_id": "vt-1", "task_status": "PENDING"}}
            )
        if request.url.path.endswith("/cancel"):
            state["cancelled"] = True
            # Mirror reality: the empty-input task usually already FAILED.
            return httpx.Response(
                400, json={"code": "UnsupportedOperation", "message": "not PENDING"}
            )
        return httpx.Response(200, json={})

    client = _client(handler, live=False)  # verify works regardless of the gate
    provider = VideoProvider(client)
    assert await provider.verify_model_available("wan2.7-t2v") is True
    assert state["submitted"] is True
    assert state["cancelled"] is True  # recognized model -> doomed task cancelled
    await client.aclose()


async def test_verify_model_available_false_on_unknown_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": "InvalidParameter", "message": "Model not exist."})

    client = _client(handler, live=False)
    provider = VideoProvider(client)
    assert await provider.verify_model_available("wan-bogus") is False
    await client.aclose()


# --------------------------------------------------------------------------- #
# Mocked render success (gate open)
# --------------------------------------------------------------------------- #


async def test_render_success_path(monkeypatch: pytest.MonkeyPatch) -> None:
    import dashscope

    class _Sub:
        status_code = 200
        code = None
        message = None
        request_id = "sub-1"
        output = {"task_id": "task-9", "task_status": "PENDING"}

    class _Done:
        status_code = 200
        code = None
        message = None
        request_id = "done-1"
        output = {"task_status": "SUCCEEDED", "video_url": "https://assets.test/clip.mp4"}

    monkeypatch.setattr(dashscope.VideoSynthesis, "async_call", lambda **kw: _Sub())
    monkeypatch.setattr(dashscope.VideoSynthesis, "fetch", lambda task_id, **kw: _Done())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "assets.test":
            return httpx.Response(200, content=b"MP4-CLIP-BYTES")
        return httpx.Response(200, json={})

    client = ProviderClient(
        _settings(live=True),
        transport=httpx.MockTransport(handler),
        resilience=ResilienceConfig(rate_per_s=1000, rate_burst=1000, backoff_base_s=0.0),
    )
    provider = VideoProvider(
        client, poll=VideoPollConfig(timeout_s=5, interval_s=0.0, max_interval_s=0.0)
    )
    result = await provider.render(
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="meadow at dawn", duration_s=5)
    )
    assert result.clip_bytes == b"MP4-CLIP-BYTES"
    assert result.clip_url == "https://assets.test/clip.mp4"
    assert result.provider_task_id == "task-9"
    assert result.duration_s == 5.0
    totals = client.usage_totals
    assert totals is not None and totals.video_seconds == 5.0
    await client.aclose()
