"""THE HEADLINE TEST: config-only onboarding of a fictional model, end to end.

A model that does not exist ("Nebula-Dream 3" / "Pulsar-1") is onboarded purely by
its descriptor file and driven through a full submit → poll → fetch render against
a mocked transport — proving a brand-new model needs zero new Python.
"""

from __future__ import annotations

import base64

import pytest

from app.core.config import Settings
from app.providers.errors import (
    LiveVideoDisabled,
    ProviderBadRequest,
    ProviderError,
    ProviderTimeout,
)
from app.providers.types import WanMode, WanSpec
from app.video.adapters.open import build_from_descriptor, load_bundled
from app.video.adapters.open.base import PollConfig
from app.video.adapters.open.descriptor_adapter import (
    ComfyUIProvider,
    DescriptorAdapter,
    OpenAPIProvider,
)

from .conftest import NO_SLEEP_POLL, RouteMap, tripwire


def _nebula(
    transport: object,
    *,
    settings: Settings,
    live: bool = True,
    poll: PollConfig = NO_SLEEP_POLL,
) -> DescriptorAdapter:
    return build_from_descriptor(
        load_bundled("fictional_nebula"),
        api_key="nebula-key",
        allow_network=True,
        live_video=live,
        settings=settings,
        transport=transport,
        poll=poll,
    )


# --------------------------------------------------------------------------- #
# Config-only onboarding: full lifecycle for a fictional model
# --------------------------------------------------------------------------- #


async def test_fictional_model_full_render_via_descriptor(settings: Settings) -> None:
    routes = (
        RouteMap()
        .json(
            "/submit",
            body={
                "data": {
                    "generation": {"handle": "gen-99"},
                    "links": {
                        "status": "https://api.nebula-dream.test/rpc/v3/renders/status/gen-99"
                    },
                }
            },
        )
        .json(
            "renders/status/gen-99",
            body={
                "data": {
                    "generation": {
                        "phase": "FINISHED",
                        "percent": 100,
                        "artifacts": {"video": {"delivery_url": "https://cdn.nebula.test/c.mp4"}},
                    }
                }
            },
        )
        .bytes("cdn.nebula.test/c.mp4", b"NEBULA-CLIP")
    )
    adapter = _nebula(routes.transport(), settings=settings)

    result = await adapter.render(
        WanSpec(
            mode=WanMode.TEXT_TO_VIDEO, prompt="a comet over snow", duration_s=6, resolution="720P"
        )
    )

    assert result.clip_bytes == b"NEBULA-CLIP"
    assert result.model == "nebula-dream-3-turbo"
    assert result.provider_task_id == "gen-99"
    assert result.clip_url == "https://cdn.nebula.test/c.mp4"
    # eager download happened exactly once
    assert routes.hits["cdn.nebula.test/c.mp4"] == 1
    await adapter.aclose()


async def test_descriptor_follows_provider_poll_url(settings: Settings) -> None:
    # The descriptor's poll_url_path returns an absolute status URL; the adapter
    # must poll *that*, not build a path from task_id.
    routes = (
        RouteMap()
        .json(
            "/submit",
            body={
                "data": {
                    "generation": {"handle": "g1"},
                    "links": {"status": "https://elsewhere.test/poll/g1"},
                }
            },
        )
        .json(
            "elsewhere.test/poll/g1",
            body={
                "data": {
                    "generation": {
                        "phase": "READY",
                        "artifacts": {"video": {"url": "https://cdn/x.mp4"}},
                    }
                }
            },
        )
        .bytes("cdn/x.mp4", b"BYTES")
    )
    adapter = _nebula(routes.transport(), settings=settings)
    await adapter.render(
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=5, resolution="720P")
    )
    assert routes.hits["elsewhere.test/poll/g1"] == 1
    await adapter.aclose()


async def test_descriptor_inline_base64_fallback(settings: Settings) -> None:
    raw = b"INLINE-MP4"
    routes = (
        RouteMap()
        .json(
            "/submit",
            body={
                "data": {
                    "generation": {"handle": "g2"},
                    "links": {"status": "https://api.nebula-dream.test/rpc/v3/renders/status/g2"},
                }
            },
        )
        .json(
            "renders/status/g2",
            body={
                "data": {
                    "generation": {
                        "phase": "FINISHED",
                        "artifacts": {"video": {"inline_base64": base64.b64encode(raw).decode()}},
                    }
                }
            },
        )
    )
    adapter = _nebula(routes.transport(), settings=settings)
    result = await adapter.render(
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=5, resolution="720P")
    )
    assert result.clip_bytes == raw  # taken inline; no download call
    await adapter.aclose()


async def test_pulsar_json_descriptor_renders(settings: Settings) -> None:
    routes = (
        RouteMap()
        .json(
            "predictions",
            body={
                "id": "pred-1",
                "urls": {"get": "https://api.pulsar-video.test/v1/predictions/pred-1"},
            },
        )
        .json(
            "predictions/pred-1",
            body={"status": "succeeded", "output": ["https://cdn.pulsar/out.mp4"]},
        )
        .bytes("cdn.pulsar/out.mp4", b"PULSAR")
    )
    adapter = build_from_descriptor(
        load_bundled("fictional_pulsar"),
        api_key="k",
        allow_network=True,
        live_video=True,
        settings=settings,
        transport=routes.transport(),
        poll=NO_SLEEP_POLL,
    )
    result = await adapter.render(
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=5, resolution="720P")
    )
    assert result.clip_bytes == b"PULSAR"
    await adapter.aclose()


# --------------------------------------------------------------------------- #
# Request shaping from the body template
# --------------------------------------------------------------------------- #


def test_submit_body_shaped_from_template(settings: Settings) -> None:
    adapter = _nebula(tripwire(), settings=settings)
    spec = WanSpec(
        mode=WanMode.IMAGE_TO_VIDEO,
        prompt="she turns",
        negative_prompt="blurry",
        image_url="https://x/first.jpg",
        seed=7,
        duration_s=6,
        resolution="720P",
    )
    body = adapter._build_submit_body(spec)
    gen = body["generation"]
    assert gen["caption"] == "she turns"
    assert gen["avoid"] == "blurry"
    assert gen["seconds"] == 6
    assert gen["determinism"]["seed"] == 7
    assert gen["driving_frame"] == "https://x/first.jpg"
    # absent reference list resolves to [] (whole-list placeholder)
    assert gen["identity_refs"] == []


def test_submit_path_interpolates_model(settings: Settings) -> None:
    adapter = _nebula(tripwire(), settings=settings)
    spec = WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=5, resolution="720P")
    assert adapter._submit_path(spec) == "renders/nebula-dream-3-turbo/submit"


# --------------------------------------------------------------------------- #
# Gates + failure normalization
# --------------------------------------------------------------------------- #


async def test_spend_gate_blocks_before_any_call(settings: Settings) -> None:
    adapter = _nebula(tripwire(), live=False, settings=settings)
    with pytest.raises(LiveVideoDisabled):
        await adapter.render(
            WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=5, resolution="720P")
        )
    await adapter.aclose()


async def test_capability_rejection_before_submit(settings: Settings) -> None:
    # 99s exceeds the 12s max → ProviderBadRequest, no network touched.
    adapter = _nebula(tripwire(), settings=settings)
    with pytest.raises(ProviderBadRequest) as ei:
        await adapter.render(
            WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=99, resolution="720P")
        )
    assert "duration" in str(ei.value)
    await adapter.aclose()


async def test_failed_task_raises_provider_error(settings: Settings) -> None:
    routes = (
        RouteMap()
        .json(
            "/submit",
            body={
                "data": {
                    "generation": {"handle": "g"},
                    "links": {"status": "https://api.nebula-dream.test/rpc/v3/renders/status/g"},
                }
            },
        )
        .json(
            "renders/status/g",
            body={"data": {"generation": {"phase": "ERRORED", "note": "gpu oom"}}},
        )
    )
    adapter = _nebula(routes.transport(), settings=settings)
    with pytest.raises(ProviderError) as ei:
        await adapter.render(
            WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=5, resolution="720P")
        )
    assert "gpu oom" in str(ei.value)
    await adapter.aclose()


async def test_poll_timeout_raises(settings: Settings) -> None:
    routes = (
        RouteMap()
        .json(
            "/submit",
            body={
                "data": {
                    "generation": {"handle": "g"},
                    "links": {"status": "https://api.nebula-dream.test/rpc/v3/renders/status/g"},
                }
            },
        )
        .json(
            "renders/status/g", body={"data": {"generation": {"phase": "RENDERING"}}}
        )  # never terminal
    )
    adapter = _nebula(
        routes.transport(),
        settings=settings,
        poll=PollConfig(timeout_s=0.0, interval_s=0.0, max_interval_s=0.0, backoff=1.0),
    )
    with pytest.raises(ProviderTimeout):
        await adapter.render(
            WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=5, resolution="720P")
        )
    await adapter.aclose()


async def test_no_task_id_raises(settings: Settings) -> None:
    routes = RouteMap().json("/submit", body={"data": {"generation": {}}})
    adapter = _nebula(routes.transport(), settings=settings)
    with pytest.raises(ProviderError) as ei:
        await adapter.render(
            WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=5, resolution="720P")
        )
    assert "task id" in str(ei.value)
    await adapter.aclose()


# --------------------------------------------------------------------------- #
# Aliases
# --------------------------------------------------------------------------- #


async def test_comfyui_alias_renders_self_hosted(settings: Settings) -> None:
    # ComfyUIProvider is the descriptor engine; the comfyui descriptor is self-hosted.
    routes = (
        RouteMap()
        .json("prompt", body={"prompt_id": "p1"})
        .json(
            "history/p1",
            body={
                "p1": {
                    "status": {"status_str": "success"},
                    "outputs": {"9": {"gifs": [{"url": "http://comfyui.local:8188/view/out.mp4"}]}},
                }
            },
        )
        .bytes("view/out.mp4", b"COMFY")
    )
    desc = load_bundled("comfyui_example")
    adapter = ComfyUIProvider.from_descriptor(
        desc,
        api_key=None,
        allow_network=True,
        live_video=True,
        settings=settings,
        transport=routes.transport(),
        poll=NO_SLEEP_POLL,
    )
    assert isinstance(adapter, DescriptorAdapter)
    result = await adapter.render(
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="dawn", duration_s=5, resolution="720P")
    )
    assert result.clip_bytes == b"COMFY"
    # self-hosted ⇒ usage records zero video-seconds
    assert adapter.capabilities().self_hosted is True
    await adapter.aclose()


def test_openapi_alias_is_descriptor_engine() -> None:
    assert OpenAPIProvider is DescriptorAdapter
    assert ComfyUIProvider is DescriptorAdapter
