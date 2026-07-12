"""Unit tests for the Wan video provider: the LiveVideoDisabled spend gate,
mode→model/param mapping, cheap (no-render) model verification, and a mocked
submit→poll→download success path."""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.providers.base import ProviderClient, ResilienceConfig
from app.providers.errors import LiveVideoDisabled
from app.providers.types import WanMode, WanSpec
from app.providers.video import VideoModelProfile, VideoPollConfig, VideoProtocol, VideoProvider
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


async def test_render_raises_when_live_video_disabled() -> None:
    submitted = {"called": False}

    def _tripwire(request: httpx.Request) -> httpx.Response:
        submitted["called"] = True
        raise AssertionError("video endpoint must NOT be invoked when the gate is closed")

    client = _client(_tripwire, live=False)
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


def test_submit_body_per_mode_legacy_profile() -> None:
    client = _client(_ok, live=True)
    provider = VideoProvider(client)
    profile = VideoModelProfile("wan2.1-i2v-turbo", VideoProtocol.LEGACY)

    t2v = provider._submit_body(
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="p", negative_prompt="bad", seed=3), profile
    )
    assert t2v["input"]["prompt"] == "p"
    assert t2v["parameters"]["negative_prompt"] == "bad"
    assert t2v["parameters"]["seed"] == 3
    assert "img_url" not in t2v["input"] and "media" not in t2v["input"]

    i2v = provider._submit_body(
        WanSpec(mode=WanMode.IMAGE_TO_VIDEO, image_url="https://x/first.png"), profile
    )
    assert i2v["input"]["img_url"] == "https://x/first.png"

    flf = provider._submit_body(
        WanSpec(
            mode=WanMode.FIRST_LAST_FRAME,
            first_frame_url="https://x/a.png",
            last_frame_url="https://x/b.png",
        ),
        profile,
    )
    assert flf["input"]["first_frame_url"] == "https://x/a.png"
    assert flf["input"]["last_frame_url"] == "https://x/b.png"

    r2v = provider._submit_body(
        WanSpec(
            mode=WanMode.REFERENCE_TO_VIDEO,
            reference_image_urls=["https://x/ref1.png", "https://x/ref2.png"],
            reference_voice_url="https://x/voice.mp3",
        ),
        profile,
    )
    assert r2v["input"]["img_url"] == "https://x/ref1.png"
    assert r2v["input"]["reference_image_urls"] == ["https://x/ref1.png", "https://x/ref2.png"]
    assert r2v["input"]["reference_voice_url"] == "https://x/voice.mp3"

    edit = provider._submit_body(
        WanSpec(
            mode=WanMode.INSTRUCTION_EDIT,
            prompt="make it red",
            source_video_url="https://x/clip.mp4",
        ),
        profile,
    )
    assert edit["input"]["video_url"] == "https://x/clip.mp4"


def test_submit_body_per_mode_media_profile() -> None:
    client = _client(_ok, live=True)
    provider = VideoProvider(client)
    profile = VideoModelProfile("wan2.7-i2v", VideoProtocol.MEDIA)

    flf = provider._submit_body(
        WanSpec(
            mode=WanMode.FIRST_LAST_FRAME,
            first_frame_url="https://x/a.png",
            last_frame_url="https://x/b.png",
        ),
        profile,
    )
    assert flf["input"]["media"] == [
        {"type": "first_frame", "url": "https://x/a.png"},
        {"type": "last_frame", "url": "https://x/b.png"},
    ]

    continuation = provider._submit_body(
        WanSpec(
            mode=WanMode.VIDEO_CONTINUATION,
            image_url="https://x/frame.png",
            source_video_url="https://x/clip.mp4",
        ),
        profile,
    )
    assert continuation["input"]["media"] == [
        {"type": "first_frame", "url": "https://x/frame.png"},
        {"type": "first_clip", "url": "https://x/clip.mp4"},
    ]


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(1, 3), (2, 3), (3, 3), (4, 4), (5, 5), (6, 5), (38, 5)],
)
def test_wan21_duration_is_snapped_to_supported_menu(
    requested: int, expected: int
) -> None:
    client = _client(_ok, live=True)
    provider = VideoProvider(client)
    profile = VideoModelProfile("wan2.1-i2v-turbo", VideoProtocol.LEGACY)

    body = provider._submit_body(
        WanSpec(mode=WanMode.IMAGE_TO_VIDEO, image_url="https://x/first.png", duration_s=requested),
        profile,
    )

    assert body["parameters"]["duration"] == expected


def test_wan27_duration_is_clamped_to_supported_range() -> None:
    client = _client(_ok, live=True)
    provider = VideoProvider(client)
    profile = VideoModelProfile("wan2.7-i2v", VideoProtocol.MEDIA)

    low = provider._submit_body(WanSpec(mode=WanMode.TEXT_TO_VIDEO, duration_s=1), profile)
    high = provider._submit_body(WanSpec(mode=WanMode.TEXT_TO_VIDEO, duration_s=38), profile)

    assert low["parameters"]["duration"] == 2
    assert high["parameters"]["duration"] == 15


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
    assert await provider.verify_model_available("wan2.1-t2v-turbo") is True
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


async def test_render_success_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/video-synthesis"):
            return httpx.Response(
                200,
                json={"request_id": "sub-1", "output": {"task_id": "task-9"}},
            )
        if request.url.path.endswith("/tasks/task-9"):
            return httpx.Response(
                200,
                json={
                    "request_id": "done-1",
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [{"url": "https://assets.test/clip.mp4"}],
                    },
                },
            )
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


async def test_render_reports_effective_wan21_duration() -> None:
    submitted: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/video-synthesis"):
            submitted.update(json.loads(request.content))
            return httpx.Response(200, json={"output": {"task_id": "task-long"}})
        if request.url.path.endswith("/tasks/task-long"):
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "video_url": "https://assets.test/clip.mp4",
                    }
                },
            )
        if request.url.host == "assets.test":
            return httpx.Response(200, content=b"MP4")
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
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="long beat", duration_s=38)
    )

    assert submitted["parameters"]["duration"] == 5  # type: ignore[index]
    assert result.duration_s == 5.0
    assert client.usage_totals is not None and client.usage_totals.video_seconds == 5.0
    await client.aclose()
