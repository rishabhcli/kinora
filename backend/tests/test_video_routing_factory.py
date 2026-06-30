"""Unit tests for the config-driven router factory: profile inference from model ids
and the build_routing_router seam. No network — the backends are never rendered."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.providers.base import ProviderClient
from app.providers.types import WanMode
from app.video.routing.factory import build_routing_router, infer_profile
from app.video.routing.policy import PolicyKind
from app.video.routing.router import RoutingVideoRouter


def test_infer_profile_turbo_is_cheap_fast_low_quality() -> None:
    p = infer_profile("wan2.1-t2v-turbo")
    assert p.cost_per_s < 2.0
    assert p.quality < 0.6
    assert p.est_latency_s < 40.0


def test_infer_profile_quality_is_pricier_slower_higher_quality() -> None:
    p = infer_profile("wan2.5-t2v-preview")
    assert p.cost_per_s > 2.0
    assert p.quality > 0.8
    assert p.est_latency_s > 50.0


def test_infer_profile_neutral_default() -> None:
    p = infer_profile("some-unlabeled-model")
    assert 1.0 < p.cost_per_s < 4.0
    assert 0.4 < p.quality < 0.9


def test_infer_profile_respects_modes() -> None:
    p = infer_profile("wan-x", modes=frozenset({WanMode.TEXT_TO_VIDEO}))
    assert p.modes == frozenset({WanMode.TEXT_TO_VIDEO})


def _client() -> ProviderClient:
    return ProviderClient(Settings(dashscope_api_key="test"))


def test_build_routing_router_wires_ids() -> None:
    router = build_routing_router(
        _client(),
        model_ids=["wan2.1-t2v-turbo", "wan2.5-t2v-preview"],
        policy_kind=PolicyKind.WEIGHTED,
        hedge=2,
    )
    assert isinstance(router, RoutingVideoRouter)
    assert router.available_names() == ["video:wan2.1-t2v-turbo", "video:wan2.5-t2v-preview"]
    # Inferred profiles are keyed by the video:<id> backend name (white-box check).
    profiles = router._profiles  # noqa: SLF001
    assert profiles.get("video:wan2.1-t2v-turbo").cost_per_s < 2.0  # turbo inferred cheap
    assert profiles.get("video:wan2.5-t2v-preview").quality > 0.8  # preview inferred quality


def test_build_routing_router_requires_ids() -> None:
    with pytest.raises(ValueError, match="at least one model id"):
        build_routing_router(_client(), model_ids=[])


def test_build_routing_router_profile_override() -> None:
    from app.video.routing.capabilities import ProviderProfile

    override = ProviderProfile(cost_per_s=9.9, quality=0.99)
    router = build_routing_router(
        _client(),
        model_ids=["wan2.1-t2v-turbo"],
        profiles={"video:wan2.1-t2v-turbo": override},
    )
    # White-box: the override wins over the inferred profile.
    assert router._profiles.get("video:wan2.1-t2v-turbo").cost_per_s == 9.9  # noqa: SLF001
