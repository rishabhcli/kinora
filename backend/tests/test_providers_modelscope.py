"""Unit tests for the ModelScope video provider: config defaults, gate,
submit-body shape, poll mapping, a mocked full success path. No network."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, dashscope_api_key="test", **overrides)  # type: ignore[arg-type]


def test_modelscope_config_defaults() -> None:
    s = _settings()
    assert s.modelscope_api_key is None
    assert s.modelscope_base_url == "https://api-inference.modelscope.cn/v1"
    assert s.modelscope_video_model == "Wan-AI/Wan2.2-T2V-A14B"
    assert s.render_granularity == "shot"


def test_modelscope_config_overrides() -> None:
    s = _settings(modelscope_api_key="ms-key", render_granularity="event")
    assert s.modelscope_api_key == "ms-key"
    assert s.render_granularity == "event"


# --------------------------------------------------------------------------- #
# Provider: gate, submit-body shape, poll status mapping, mocked success path
# --------------------------------------------------------------------------- #

from app.providers.base import ProviderClient, ResilienceConfig  # noqa: E402
from app.providers.errors import LiveVideoDisabled, ProviderBadRequest  # noqa: E402
from app.providers.modelscope import ModelScopeVideoProvider  # noqa: E402
from app.providers.types import WanMode, WanSpec  # noqa: E402

_FAST = ResilienceConfig(
    max_attempts=2, backoff_base_s=0.0, backoff_max_s=0.0, backoff_jitter_s=0.0,
    breaker_failure_threshold=3, breaker_recovery_s=0.05,
    rate_per_s=1000.0, rate_burst=1000,
)


def _ms_settings(*, live: bool) -> Settings:
    return Settings(
        _env_file=None, dashscope_api_key="test", kinora_live_video=live,
        modelscope_api_key="ms-key",
    )


def _ms_client(handler: object, *, live: bool) -> ProviderClient:
    return ProviderClient(
        _ms_settings(live=live),
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        resilience=_FAST,
        base_url_override="https://api-inference.modelscope.cn/v1",
        api_key_override="ms-key",
    )


async def test_render_raises_when_live_video_disabled() -> None:
    def _tripwire(request: httpx.Request) -> httpx.Response:
        raise AssertionError("ModelScope must NOT be called when the gate is off")

    client = _ms_client(_tripwire, live=False)
    provider = ModelScopeVideoProvider(client)

    with pytest.raises(LiveVideoDisabled):
        await provider.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="a quiet meadow"))
    await client.aclose()


async def test_healthy_is_true_without_network_when_gate_off() -> None:
    def _tripwire(request: httpx.Request) -> httpx.Response:
        raise AssertionError("healthy() must not call the network when gated off")

    client = _ms_client(_tripwire, live=False)
    provider = ModelScopeVideoProvider(client)
    assert await provider.healthy() is True
    await client.aclose()


def test_submit_body_text_to_video() -> None:
    client = _ms_client(lambda r: httpx.Response(200, json={}), live=True)
    provider = ModelScopeVideoProvider(client)
    body = provider._submit_body(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="meadow at dawn"))
    assert body == {"model": "Wan-AI/Wan2.2-T2V-A14B", "prompt": "meadow at dawn"}
    # cleanup is sync-safe; no await needed for the unused client transport


def test_submit_body_rejects_non_text_to_video_mode() -> None:
    client = _ms_client(lambda r: httpx.Response(200, json={}), live=True)
    provider = ModelScopeVideoProvider(client)
    with pytest.raises(ProviderBadRequest):
        provider._submit_body(
            WanSpec(
                mode=WanMode.IMAGE_TO_VIDEO,
                prompt="she turns",
                image_url="https://x/first.jpg",
            )
        )


def test_map_status() -> None:
    assert ModelScopeVideoProvider._map_status("SUCCEED") == "ok"
    assert ModelScopeVideoProvider._map_status("FAILED") == "fail"
    assert ModelScopeVideoProvider._map_status("PENDING") == "pending"
    assert ModelScopeVideoProvider._map_status("RUNNING") == "pending"


# NOTE: this success-path test is written against ModelScope's CONFIRMED
# (verified 2026-07-04 via web search + reading a real client implementation)
# async **image**-generation contract, used here as the video analog, because no
# MODELSCOPE_API_TOKEN has ever been available in this environment to run Task 1's
# probe script (backend/scripts/probe_modelscope_video.py) against the real video
# endpoint. See the "UNCONFIRMED" banner in app/providers/modelscope.py's module
# docstring for exactly which pieces below (the "/videos/generations" path, the
# "output_videos" response field) are a guess-by-analogy rather than a verified
# fact, and re-check them there once a real token is available.
async def test_render_success_path_downloads_and_records_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/videos/generations") and request.method == "POST":
            assert request.headers.get("X-ModelScope-Async-Mode") == "true"
            return httpx.Response(200, json={"task_id": "ms-task-1"})
        if path.endswith("/tasks/ms-task-1") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "task_status": "SUCCEED",
                    "output_videos": ["https://cdn.modelscope/clip.mp4"],
                },
            )
        if request.url.host == "cdn.modelscope":
            return httpx.Response(200, content=b"MODELSCOPE-MP4-BYTES")
        return httpx.Response(200, json={})

    client = _ms_client(handler, live=True)
    provider = ModelScopeVideoProvider(client, poll_interval_s=0.0, poll_timeout_s=5.0)
    result = await provider.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="a quiet meadow"))
    assert result.clip_bytes == b"MODELSCOPE-MP4-BYTES"
    assert result.clip_url == "https://cdn.modelscope/clip.mp4"
    assert result.provider_task_id == "ms-task-1"
    assert result.duration_s == 5.0  # WanSpec's default duration_s
    assert result.model == "Wan-AI/Wan2.2-T2V-A14B"
    totals = client.usage_totals
    assert totals is not None and totals.video_seconds == 5.0
    await client.aclose()
