"""Registry/factory tests + integration proof that a frontier adapter is a drop-in
``VideoBackend`` for the existing ``VideoRouter`` (failover across providers).
No network, no keys."""

from __future__ import annotations

import httpx
import pytest

from app.providers.types import WanMode, WanSpec
from app.providers.video_router import RouteMode, RouterPolicy, VideoBackend, VideoRouter
from app.video.adapters.frontier import (
    BaseFrontierAdapter,
    UniversalVideoProvider,
    VideoMode,
    adapters_supporting,
    available_slugs,
    build_adapter,
    build_configured_adapters,
    build_runway_adapter,
    build_sora_adapter,
    capability_catalog,
    configured_slugs,
)

from .frontier_video_helpers import RecordingHandler, frontier_settings, make_transport


def test_available_and_capability_catalog() -> None:
    s = frontier_settings()
    assert available_slugs() == ["kling", "luma", "pika", "runway", "sora", "veo"]
    cat = capability_catalog(s)
    assert set(cat) == set(available_slugs())
    for prof in cat.values():
        assert prof.durations_s  # every provider declares a duration menu
        assert prof.modes


def test_configured_slugs_respects_flag_and_keys() -> None:
    assert configured_slugs(frontier_settings(enabled=False, runway_api_key="k")) == []
    s = frontier_settings(enabled=True, runway_api_key="k", veo_api_key="g")
    assert configured_slugs(s) == ["runway", "veo"]


def test_build_configured_adapters() -> None:
    s = frontier_settings(enabled=True, pika_api_key="k", luma_api_key="k2")
    adapters = build_configured_adapters(s)
    assert sorted(a.provider_slug for a in adapters) == ["luma", "pika"]
    assert all(isinstance(a, BaseFrontierAdapter) for a in adapters)


def test_build_adapter_unknown_slug_raises() -> None:
    with pytest.raises(KeyError):
        build_adapter("nope", frontier_settings())


def test_adapters_supporting_filters_by_mode() -> None:
    s = frontier_settings()
    runway = build_runway_adapter(
        s, transport=make_transport(lambda r: httpx.Response(200, json={}), provider="runway")
    )
    sora = build_sora_adapter(
        s, transport=make_transport(lambda r: httpx.Response(200, json={}), provider="sora")
    )
    # neither runway nor sora supports first-last-frame
    assert adapters_supporting(VideoMode.FIRST_LAST_FRAME, [runway, sora]) == []
    # both support image-to-video
    assert len(adapters_supporting(VideoMode.IMAGE_TO_VIDEO, [runway, sora])) == 2


def test_adapter_satisfies_protocols() -> None:
    s = frontier_settings()
    adapter = build_runway_adapter(
        s, transport=make_transport(lambda r: httpx.Response(200, json={}), provider="runway")
    )
    assert isinstance(adapter, UniversalVideoProvider)
    assert isinstance(adapter, VideoBackend)


async def test_router_fails_over_between_frontier_adapters() -> None:
    s = frontier_settings()
    # Runway transport always 503s (retryable, exhausts → router fails it over).
    runway = build_runway_adapter(
        s,
        transport=make_transport(
            lambda r: httpx.Response(503, json={"message": "down"}), provider="runway"
        ),
        poll_interval_s=0.0,
        poll_max_interval_s=0.0,
    )
    # Sora succeeds.
    sora_handler = RecordingHandler(
        {
            "videos": httpx.Response(200, json={"id": "s1", "status": "completed"}),
            "videos/s1": httpx.Response(200, json={"id": "s1", "status": "completed"}),
            "videos/s1/content": httpx.Response(200, content=b"SORA"),
        }
    )
    sora = build_sora_adapter(
        s,
        transport=make_transport(sora_handler, provider="sora"),
        poll_interval_s=0.0,
        poll_max_interval_s=0.0,
    )
    router = VideoRouter([runway, sora], policy=RouterPolicy(mode=RouteMode.FAILOVER))
    result = await router.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=4))
    assert result.clip_bytes == b"SORA"
    # runway breaker recorded the failure
    assert router.health("runway:gen4_turbo").total_failures >= 1


async def test_router_propagates_live_disabled_unchanged() -> None:
    from app.providers.errors import LiveVideoDisabled

    s = frontier_settings(live=False)
    runway = build_runway_adapter(
        s, transport=make_transport(lambda r: httpx.Response(200, json={}), provider="runway")
    )
    router = VideoRouter([runway])
    with pytest.raises(LiveVideoDisabled):
        await router.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x"))
