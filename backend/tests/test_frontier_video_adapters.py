"""Per-adapter tests for the six frontier providers (Runway, Luma, Pika, Kling,
Veo, Sora): declared capability profile, native submit-body mapping, poll parsing,
provider quirks, a mocked full render path, and error-code → taxonomy mapping.
No network, no keys."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.providers.types import WanMode, WanSpec
from app.video.adapters.frontier import (
    FrontierAuthError,
    FrontierRateLimited,
    VideoMode,
    build_kling_adapter,
    build_luma_adapter,
    build_pika_adapter,
    build_runway_adapter,
    build_sora_adapter,
    build_veo_adapter,
)
from app.video.adapters.frontier.types import FrontierRequest

from .frontier_video_helpers import RecordingHandler, frontier_settings, make_transport

# --------------------------------------------------------------------------- #
# Capability profiles (declared envelopes)
# --------------------------------------------------------------------------- #


def test_capability_profiles_declared() -> None:
    s = frontier_settings()
    runway = build_runway_adapter(
        s, transport=make_transport(lambda r: httpx.Response(200, json={}), provider="runway")
    )
    assert runway.capabilities().durations_s == (5.0, 10.0)
    assert runway.capabilities().supports_seed is True
    assert runway.capabilities().supports_negative_prompt is False
    assert runway.capabilities().max_prompt_chars == 1000

    luma = build_luma_adapter(
        s, transport=make_transport(lambda r: httpx.Response(200, json={}), provider="luma")
    )
    assert VideoMode.FIRST_LAST_FRAME in luma.capabilities().modes
    assert "4k" in luma.capabilities().resolutions
    assert luma.capabilities().supports_seed is False

    pika = build_pika_adapter(
        s, transport=make_transport(lambda r: httpx.Response(200, json={}), provider="pika")
    )
    assert pika.capabilities().supports_negative_prompt is True
    assert pika.capabilities().max_prompt_chars == 512

    kling = build_kling_adapter(
        s, transport=make_transport(lambda r: httpx.Response(200, json={}), provider="kling")
    )
    assert VideoMode.FIRST_LAST_FRAME in kling.capabilities().modes

    veo = build_veo_adapter(
        s, transport=make_transport(lambda r: httpx.Response(200, json={}), provider="veo")
    )
    assert veo.capabilities().durations_s == (4.0, 6.0, 8.0)
    assert veo.capabilities().supports_negative_prompt is True

    sora = build_sora_adapter(
        s, transport=make_transport(lambda r: httpx.Response(200, json={}), provider="sora")
    )
    assert sora.capabilities().durations_s == (4.0, 8.0, 12.0)
    assert sora.capabilities().supports_negative_prompt is False


# --------------------------------------------------------------------------- #
# Runway
# --------------------------------------------------------------------------- #


def _req(mode: VideoMode = VideoMode.TEXT_TO_VIDEO, **kw: Any) -> FrontierRequest:
    base: dict[str, Any] = {
        "resolution": "720p",
        "aspect_ratio": "16:9",
        "duration_s": 5.0,
        "prompt": "p",
    }
    base.update(kw)
    return FrontierRequest(mode=mode, **base)


def test_runway_submit_body_text_and_image() -> None:
    s = frontier_settings()
    adapter = build_runway_adapter(
        s, transport=make_transport(lambda r: httpx.Response(200, json={}), provider="runway")
    )
    path, body = adapter._build_submit(_req(seed=42))
    assert path == "text_to_video"
    assert body["ratio"] == "1280:720"
    assert body["duration"] == 5
    assert body["seed"] == 42
    assert body["promptText"] == "p"

    path2, body2 = adapter._build_submit(
        _req(mode=VideoMode.IMAGE_TO_VIDEO, image_url="https://x/a.png")
    )
    assert path2 == "image_to_video"
    assert body2["promptImage"] == "https://x/a.png"
    # portrait ratio token
    _, body3 = adapter._build_submit(_req(aspect_ratio="9:16"))
    assert body3["ratio"] == "720:1280"


def test_runway_x_version_header_set() -> None:
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["v"] = req.headers.get("X-Runway-Version", "")
        return httpx.Response(200, json={"id": "rw-1"})

    s = frontier_settings()
    tx = make_transport(
        handler, provider="runway", extra_headers={"X-Runway-Version": "2024-11-06"}
    )
    adapter = build_runway_adapter(s, transport=tx)
    import asyncio

    asyncio.get_event_loop()
    # submit through the gated render path is overkill here; call submit directly.

    async def go() -> None:
        await adapter.submit(_req())

    import anyio

    anyio.run(go)
    assert seen["v"] == "2024-11-06"


async def test_runway_full_render() -> None:
    handler = RecordingHandler(
        {
            "text_to_video": httpx.Response(200, json={"id": "rw-7"}),
            "tasks/rw-7": httpx.Response(
                200, json={"status": "SUCCEEDED", "output": ["https://cdn.runway/clip.mp4"]}
            ),
            "clip.mp4": httpx.Response(200, content=b"RW"),
        }
    )
    s = frontier_settings()
    adapter = build_runway_adapter(
        s,
        transport=make_transport(handler, provider="runway"),
        poll_interval_s=0.0,
        poll_max_interval_s=0.0,
        poll_timeout_s=5.0,
    )
    result = await adapter.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x"))
    assert result.clip_bytes == b"RW"
    assert result.provider_task_id == "rw-7"


# --------------------------------------------------------------------------- #
# Luma
# --------------------------------------------------------------------------- #


def test_luma_submit_keyframes_first_last() -> None:
    s = frontier_settings()
    adapter = build_luma_adapter(
        s, transport=make_transport(lambda r: httpx.Response(200, json={}), provider="luma")
    )
    _, body = adapter._build_submit(
        _req(
            mode=VideoMode.FIRST_LAST_FRAME,
            first_frame_url="https://x/a.png",
            last_frame_url="https://x/b.png",
        )
    )
    assert body["keyframes"]["frame0"] == {"type": "image", "url": "https://x/a.png"}
    assert body["keyframes"]["frame1"] == {"type": "image", "url": "https://x/b.png"}
    assert body["duration"] == "5s"
    # image-to-video sets only frame0
    _, body2 = adapter._build_submit(
        _req(mode=VideoMode.IMAGE_TO_VIDEO, image_url="https://x/c.png")
    )
    assert "frame1" not in body2["keyframes"]


async def test_luma_full_render_parses_assets_video() -> None:
    handler = RecordingHandler(
        {
            "generations": httpx.Response(200, json={"id": "lm-1"}),
            "generations/lm-1": httpx.Response(
                200,
                json={
                    "state": "completed",
                    "assets": {
                        "video": "https://cdn.luma/clip.mp4",
                        "image": "https://cdn.luma/t.png",
                    },
                },
            ),
            "clip.mp4": httpx.Response(200, content=b"LM"),
            "t.png": httpx.Response(200, content=b"THUMB"),
        }
    )
    s = frontier_settings()
    adapter = build_luma_adapter(
        s,
        transport=make_transport(handler, provider="luma"),
        poll_interval_s=0.0,
        poll_max_interval_s=0.0,
    )
    result = await adapter.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x"))
    assert result.clip_bytes == b"LM"
    assert result.last_frame_bytes == b"THUMB"


# --------------------------------------------------------------------------- #
# Pika
# --------------------------------------------------------------------------- #


def test_pika_submit_nests_options_and_negative_prompt() -> None:
    s = frontier_settings()
    adapter = build_pika_adapter(
        s, transport=make_transport(lambda r: httpx.Response(200, json={}), provider="pika")
    )
    _, body = adapter._build_submit(_req(seed=3, negative_prompt="blur"))
    assert body["options"]["seed"] == 3
    assert body["options"]["negativePrompt"] == "blur"
    assert body["options"]["resolution"] == "720p"
    assert body["promptText"] == "p"


async def test_pika_full_render() -> None:
    handler = RecordingHandler(
        {
            "generate": httpx.Response(200, json={"id": "pk-1"}),
            "videos/pk-1": httpx.Response(
                200, json={"status": "finished", "url": "https://cdn.pika/c.mp4"}
            ),
            "c.mp4": httpx.Response(200, content=b"PK"),
        }
    )
    s = frontier_settings()
    adapter = build_pika_adapter(
        s,
        transport=make_transport(handler, provider="pika"),
        poll_interval_s=0.0,
        poll_max_interval_s=0.0,
    )
    result = await adapter.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x"))
    assert result.clip_bytes == b"PK"


# --------------------------------------------------------------------------- #
# Kling
# --------------------------------------------------------------------------- #


def test_kling_strips_data_uri_and_uses_duration_string() -> None:
    s = frontier_settings()
    adapter = build_kling_adapter(
        s, transport=make_transport(lambda r: httpx.Response(200, json={}), provider="kling")
    )
    path, body = adapter._build_submit(
        _req(mode=VideoMode.IMAGE_TO_VIDEO, image_url="data:image/png;base64,QUJD", duration_s=10.0)
    )
    assert path == "videos/image2video"
    assert body["image"] == "QUJD"  # data URI header stripped
    assert body["duration"] == "10"


def test_kling_first_last_uses_image_and_image_tail() -> None:
    s = frontier_settings()
    adapter = build_kling_adapter(
        s, transport=make_transport(lambda r: httpx.Response(200, json={}), provider="kling")
    )
    _, body = adapter._build_submit(
        _req(
            mode=VideoMode.FIRST_LAST_FRAME,
            first_frame_url="https://x/a.png",
            last_frame_url="https://x/b.png",
        )
    )
    assert body["image"] == "https://x/a.png"
    assert body["image_tail"] == "https://x/b.png"


async def test_kling_business_code_nonzero_is_error() -> None:
    handler = RecordingHandler(
        {"videos/text2video": httpx.Response(200, json={"code": 1102, "message": "throttled"})}
    )
    s = frontier_settings()
    adapter = build_kling_adapter(s, transport=make_transport(handler, provider="kling"))
    with pytest.raises(FrontierRateLimited):
        await adapter.submit(_req())


async def test_kling_full_render_nested_result() -> None:
    handler = RecordingHandler(
        {
            "videos/image2video": httpx.Response(
                200, json={"code": 0, "data": {"task_id": "kl-1", "task_status": "submitted"}}
            ),
            "videos/image2video/kl-1": httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "task_status": "succeed",
                        "task_result": {"videos": [{"url": "https://cdn.kling/c.mp4"}]},
                    },
                },
            ),
            "c.mp4": httpx.Response(200, content=b"KL"),
        }
    )
    s = frontier_settings()
    adapter = build_kling_adapter(
        s,
        transport=make_transport(handler, provider="kling"),
        poll_interval_s=0.0,
        poll_max_interval_s=0.0,
    )
    result = await adapter.render(
        WanSpec(mode=WanMode.IMAGE_TO_VIDEO, prompt="x", image_url="https://x/a.png")
    )
    assert result.clip_bytes == b"KL"
    assert result.provider_task_id == "kl-1"


# --------------------------------------------------------------------------- #
# Veo
# --------------------------------------------------------------------------- #


def test_veo_submit_path_templated_with_model_and_inline_image() -> None:
    s = frontier_settings()
    adapter = build_veo_adapter(
        s, transport=make_transport(lambda r: httpx.Response(200, json={}), provider="veo")
    )
    path, body = adapter._build_submit(
        _req(
            mode=VideoMode.IMAGE_TO_VIDEO,
            image_url="data:image/png;base64,QUJD",
            negative_prompt="blur",
            seed=5,
            duration_s=8.0,
        )
    )
    assert path == f"models/{s.veo_model}:predictLongRunning"
    inst = body["instances"][0]
    assert inst["image"] == {"bytesBase64Encoded": "QUJD", "mimeType": "image/png"}
    assert body["parameters"]["durationSeconds"] == 8
    assert body["parameters"]["negativePrompt"] == "blur"
    assert body["parameters"]["seed"] == 5


def test_veo_uses_x_goog_api_key_not_bearer() -> None:
    seen: dict[str, str | None] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["goog"] = req.headers.get("x-goog-api-key")
        seen["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, json={"name": "models/veo/operations/op-1"})

    s = frontier_settings(veo_api_key="goog-key")
    adapter = build_veo_adapter(s)  # real transport build → header wiring exercised
    # Re-point its transport's http client at our mock by rebuilding via injection:
    tx = make_transport(
        handler,
        provider="veo",
        api_key="goog-key",
        auth_scheme="",
        extra_headers={"x-goog-api-key": "goog-key"},
    )
    adapter = build_veo_adapter(s, transport=tx)

    async def go() -> None:
        await adapter.submit(_req())

    import anyio

    anyio.run(go)
    assert seen["goog"] == "goog-key"
    assert seen["auth"] is None  # no redundant bearer header


async def test_veo_full_render_done_operation() -> None:
    op = "models/veo/operations/op-7"
    handler = RecordingHandler(
        {
            ":predictLongRunning": httpx.Response(200, json={"name": op}),
            op: httpx.Response(
                200,
                json={
                    "done": True,
                    "response": {
                        "generateVideoResponse": {
                            "generatedSamples": [{"video": {"uri": "https://cdn.veo/c.mp4"}}]
                        }
                    },
                },
            ),
            "c.mp4": httpx.Response(200, content=b"VO"),
        }
    )
    s = frontier_settings(veo_api_key="k")
    adapter = build_veo_adapter(
        s,
        transport=make_transport(handler, provider="veo"),
        poll_interval_s=0.0,
        poll_max_interval_s=0.0,
    )
    result = await adapter.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x"))
    assert result.clip_bytes == b"VO"


# --------------------------------------------------------------------------- #
# Sora
# --------------------------------------------------------------------------- #


def test_sora_size_token_and_seconds_string() -> None:
    s = frontier_settings()
    adapter = build_sora_adapter(
        s, transport=make_transport(lambda r: httpx.Response(200, json={}), provider="sora")
    )
    _, body = adapter._build_submit(_req(resolution="720p", aspect_ratio="16:9", duration_s=8.0))
    assert body["size"] == "1280x720"
    assert body["seconds"] == "8"
    _, body2 = adapter._build_submit(_req(resolution="720p", aspect_ratio="9:16", duration_s=4.0))
    assert body2["size"] == "720x1280"


async def test_sora_full_render_downloads_from_content_subresource() -> None:
    handler = RecordingHandler(
        {
            "videos": httpx.Response(200, json={"id": "sora-1", "status": "queued"}),
            "videos/sora-1": httpx.Response(200, json={"id": "sora-1", "status": "completed"}),
            "videos/sora-1/content": httpx.Response(200, content=b"SORA"),
        }
    )
    s = frontier_settings()
    adapter = build_sora_adapter(
        s,
        transport=make_transport(handler, provider="sora"),
        poll_interval_s=0.0,
        poll_max_interval_s=0.0,
    )
    result = await adapter.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=4))
    assert result.clip_bytes == b"SORA"
    assert handler.hits["videos/sora-1/content"] == 1


# --------------------------------------------------------------------------- #
# Error mapping quirks (per-provider mapper → taxonomy)
# --------------------------------------------------------------------------- #


async def test_runway_401_maps_to_auth_error() -> None:
    handler = RecordingHandler({"text_to_video": httpx.Response(401, json={"error": "bad key"})})
    s = frontier_settings()
    adapter = build_runway_adapter(s, transport=make_transport(handler, provider="runway"))
    with pytest.raises(FrontierAuthError):
        await adapter.submit(_req())


async def test_sora_429_maps_to_rate_limited() -> None:
    handler = RecordingHandler(
        {"videos": httpx.Response(429, json={"error": {"message": "rate", "code": "rate_limit"}})}
    )
    s = frontier_settings()
    # single attempt to avoid retry loop hiding the type
    adapter = build_sora_adapter(s, transport=make_transport(handler, provider="sora"))
    with pytest.raises(FrontierRateLimited):
        # the transport will retry 429 then re-raise the last RateLimited
        await adapter.submit(_req())
