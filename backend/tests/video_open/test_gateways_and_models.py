"""Gateway meta-adapters (Replicate, fal) + the named open models + Stability SVD."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.providers.errors import LiveVideoDisabled, ProviderError
from app.providers.types import WanMode, WanSpec
from app.video.adapters.open import (
    CogVideoXProvider,
    FalProvider,
    HunyuanVideoProvider,
    InputMap,
    LTXVideoProvider,
    MochiProvider,
    ReplicateProvider,
    StableVideoDiffusionProvider,
    mochi_capabilities,
)

from .conftest import NO_SLEEP_POLL, RouteMap, tripwire

# --------------------------------------------------------------------------- #
# Replicate meta-adapter
# --------------------------------------------------------------------------- #


async def test_replicate_full_render(settings: Settings) -> None:
    routes = (
        RouteMap()
        .json(
            "predictions",
            body={"id": "r1", "urls": {"get": "https://api.replicate.com/v1/predictions/r1"}},
        )
        .json(
            "predictions/r1",
            body={"status": "succeeded", "output": ["https://cdn.replicate/out.mp4"]},
        )
        .bytes("cdn.replicate/out.mp4", b"REPLICATE")
    )
    adapter = ReplicateProvider.build(
        version="abc123",
        capabilities=mochi_capabilities("repl"),
        api_key="r8_xxx",
        allow_network=True,
        live_video=True,
        settings=settings,
        transport=routes.transport(),
        poll=NO_SLEEP_POLL,
        model_label="repl",
    )
    result = await adapter.render(
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=5, resolution="480P")
    )
    assert result.clip_bytes == b"REPLICATE"
    # submission body carries version + input
    body = adapter._build_submit_body(
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="hi", seed=3, duration_s=5, resolution="480P")
    )
    assert body["version"] == "abc123"
    assert body["input"]["prompt"] == "hi"
    assert body["input"]["seed"] == 3
    await adapter.aclose()


async def test_replicate_token_auth_header_sent(settings: Settings) -> None:
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("predictions"):
            seen["auth"] = req.headers.get("Authorization", "")  # capture on submit
            return httpx.Response(
                200,
                json={"id": "r1", "urls": {"get": "https://api.replicate.com/v1/predictions/r1"}},
            )
        if "predictions/r1" in str(req.url):
            return httpx.Response(200, json={"status": "succeeded", "output": "https://cdn/x.mp4"})
        return httpx.Response(200, content=b"X")

    adapter = ReplicateProvider.build(
        version="v",
        capabilities=mochi_capabilities(),
        api_key="r8_secret",
        allow_network=True,
        live_video=True,
        settings=settings,
        transport=httpx.MockTransport(handler),
        poll=NO_SLEEP_POLL,
    )
    await adapter.render(
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=5, resolution="480P")
    )
    assert seen["auth"] == "Token r8_secret"  # Replicate's classic scheme
    await adapter.aclose()


async def test_replicate_failed_status(settings: Settings) -> None:
    routes = (
        RouteMap()
        .json(
            "predictions",
            body={"id": "r1", "urls": {"get": "https://api.replicate.com/v1/predictions/r1"}},
        )
        .json("predictions/r1", body={"status": "failed", "error": "nsfw"})
    )
    adapter = ReplicateProvider.build(
        version="v",
        capabilities=mochi_capabilities(),
        api_key="k",
        allow_network=True,
        live_video=True,
        settings=settings,
        transport=routes.transport(),
        poll=NO_SLEEP_POLL,
    )
    with pytest.raises(ProviderError) as ei:
        await adapter.render(
            WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=5, resolution="480P")
        )
    assert "nsfw" in str(ei.value)
    await adapter.aclose()


def test_replicate_conditioning_image_to_video(settings: Settings) -> None:
    from app.video.adapters.open import cogvideox_capabilities

    adapter = ReplicateProvider.build(
        version="v",
        capabilities=cogvideox_capabilities(),
        api_key="k",
        allow_network=True,
        live_video=True,
        settings=settings,
        transport=tripwire(),
        input_map=InputMap(image="image"),
    )
    body = adapter._build_submit_body(
        WanSpec(
            mode=WanMode.IMAGE_TO_VIDEO,
            prompt="x",
            image_url="https://i/f.jpg",
            duration_s=5,
            resolution="480P",
        )
    )
    assert body["input"]["image"] == "https://i/f.jpg"


# --------------------------------------------------------------------------- #
# fal meta-adapter (queue + separate result endpoint)
# --------------------------------------------------------------------------- #


async def test_fal_render_with_result_endpoint(settings: Settings) -> None:
    routes = (
        RouteMap()
        .json(
            "fal-ai/test-model",
            body={
                "request_id": "req-1",
                "status_url": "https://queue.fal.run/fal-ai/test-model/requests/req-1/status",
            },
        )
        .json("requests/req-1/status", body={"status": "COMPLETED"})  # status has no URL
        .json("requests/req-1", body={"video": {"url": "https://cdn.fal/out.mp4"}})  # result has it
        .bytes("cdn.fal/out.mp4", b"FALCLIP")
    )
    adapter = FalProvider.build(
        app_id="fal-ai/test-model",
        capabilities=mochi_capabilities("fal"),
        api_key="fal_key",
        allow_network=True,
        live_video=True,
        settings=settings,
        transport=routes.transport(),
        poll=NO_SLEEP_POLL,
    )
    result = await adapter.render(
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=5, resolution="480P")
    )
    assert result.clip_bytes == b"FALCLIP"
    assert routes.hits["requests/req-1"] == 1  # result endpoint consulted
    await adapter.aclose()


async def test_fal_key_auth_header(settings: Settings) -> None:
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("fal-ai/m"):
            seen["auth"] = req.headers.get("Authorization", "")  # capture on submit
            return httpx.Response(
                200,
                json={
                    "request_id": "q",
                    "status_url": "https://queue.fal.run/fal-ai/m/requests/q/status",
                },
            )
        if "status" in str(req.url):
            return httpx.Response(
                200, json={"status": "COMPLETED", "video": {"url": "https://cdn/x.mp4"}}
            )
        return httpx.Response(200, content=b"X")

    adapter = FalProvider.build(
        app_id="fal-ai/m",
        capabilities=mochi_capabilities(),
        api_key="fkey",
        allow_network=True,
        live_video=True,
        settings=settings,
        transport=httpx.MockTransport(handler),
        poll=NO_SLEEP_POLL,
    )
    await adapter.render(
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=5, resolution="480P")
    )
    assert seen["auth"] == "Key fkey"
    await adapter.aclose()


# --------------------------------------------------------------------------- #
# Named open models: capability profiles + gateway wiring
# --------------------------------------------------------------------------- #


def test_named_model_capability_profiles() -> None:
    assert (
        WanMode.TEXT_TO_VIDEO
        in MochiProvider.build(version="v", api_key="k", allow_network=False, live_video=False)
        .capabilities()
        .modes
    )
    cog = CogVideoXProvider.build(version="v", api_key="k", allow_network=False, live_video=False)
    assert {WanMode.TEXT_TO_VIDEO, WanMode.IMAGE_TO_VIDEO} <= cog.capabilities().modes
    ltx = LTXVideoProvider.build(version="v", api_key="k", allow_network=False, live_video=False)
    assert WanMode.FIRST_LAST_FRAME in ltx.capabilities().modes
    hun = HunyuanVideoProvider.build(api_key="k", allow_network=False, live_video=False)
    assert hun.capabilities().quality > 0.7
    assert hun.provider_id == "fal"
    assert cog.provider_id == "replicate"


async def test_cogvideox_renders_on_replicate(settings: Settings) -> None:
    routes = (
        RouteMap()
        .json(
            "predictions",
            body={"id": "c1", "urls": {"get": "https://api.replicate.com/v1/predictions/c1"}},
        )
        .json("predictions/c1", body={"status": "succeeded", "output": "https://cdn/cog.mp4"})
        .bytes("cdn/cog.mp4", b"COG")
    )
    adapter = CogVideoXProvider.build(
        version="cogver",
        api_key="k",
        allow_network=True,
        live_video=True,
        settings=settings,
        transport=routes.transport(),
        poll=NO_SLEEP_POLL,
    )
    result = await adapter.render(
        WanSpec(
            mode=WanMode.IMAGE_TO_VIDEO,
            prompt="x",
            image_url="https://i/f.jpg",
            duration_s=5,
            resolution="720P",
        )
    )
    assert result.clip_bytes == b"COG"
    await adapter.aclose()


async def test_named_model_honours_spend_gate(settings: Settings) -> None:
    adapter = MochiProvider.build(
        version="v",
        api_key="k",
        allow_network=True,
        live_video=False,
        settings=settings,
        transport=tripwire(),
    )
    with pytest.raises(LiveVideoDisabled):
        await adapter.render(
            WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=5, resolution="480P")
        )
    await adapter.aclose()


# --------------------------------------------------------------------------- #
# Stability SVD (native image-to-video API, inline bytes)
# --------------------------------------------------------------------------- #


async def test_svd_image_to_video_inline_bytes(settings: Settings) -> None:
    import base64

    raw = b"SVD-MP4"
    # The poll path "image-to-video/result/gen-svd" contains the submit substring;
    # the RouteMap's longest-match resolves that correctly.
    routes = (
        RouteMap()
        .json("v2beta/image-to-video", body={"id": "gen-svd"})
        .json("image-to-video/result/gen-svd", body={"video": base64.b64encode(raw).decode()})
    )
    adapter = StableVideoDiffusionProvider.build(
        api_key="sk",
        allow_network=True,
        live_video=True,
        settings=settings,
        transport=routes.transport(),
        poll=NO_SLEEP_POLL,
    )
    result = await adapter.render(
        WanSpec(
            mode=WanMode.IMAGE_TO_VIDEO,
            image_url="https://i/f.jpg",
            duration_s=3,
            resolution="720P",
        )
    )
    assert result.clip_bytes == raw
    await adapter.aclose()


async def test_svd_rejects_text_to_video(settings: Settings) -> None:
    from app.providers.errors import ProviderBadRequest

    adapter = StableVideoDiffusionProvider.build(
        api_key="sk",
        allow_network=True,
        live_video=True,
        settings=settings,
        transport=tripwire(),
    )
    with pytest.raises(ProviderBadRequest):
        await adapter.render(
            WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=3, resolution="720P")
        )
    await adapter.aclose()
