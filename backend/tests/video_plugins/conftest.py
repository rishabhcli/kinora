"""Shared fixtures + fake plugins for the SDK tests.

Everything here is deterministic and offline: the fake plugins never touch the
network, never spend, and never read ambient state. They are the stand-ins a
third-party plugin author would ship, exercised through the real SDK pipeline.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.video.plugins.contracts import (
    PLUGIN_API_VERSION,
    CapabilityProfile,
    ProbeResult,
    RenderMode,
    VideoArtifact,
    VideoProviderPlugin,
    VideoRequest,
)
from app.video.plugins.manifest import PluginManifest

_T2V_PROFILE = CapabilityProfile(
    modes=frozenset({RenderMode.TEXT_TO_VIDEO}),
    resolutions=frozenset({"720P"}),
    min_duration_s=1.0,
    max_duration_s=10.0,
    supports_negative_prompt=True,
    supports_seed=True,
)


class GoodPlugin:
    """A contract-satisfying plugin that passes the default conformance contract."""

    capabilities = _T2V_PROFILE

    def __init__(self, *, config: dict[str, Any], host: object) -> None:
        self._config = config
        self._host = host

    async def probe(self) -> ProbeResult:
        return ProbeResult(healthy=True, detail="ok")

    async def generate(self, request: VideoRequest) -> VideoArtifact:
        return VideoArtifact(
            clip_url="https://example.invalid/clip.mp4",
            duration_s=request.duration_s,
            model="good.model",
            mode=request.mode,
        )


class BrokenGeneratePlugin:
    """A plugin whose ``generate`` raises — must be quarantined, not crash the host."""

    capabilities = _T2V_PROFILE

    def __init__(self, *, config: dict[str, Any], host: object) -> None: ...

    async def probe(self) -> ProbeResult:
        return ProbeResult(healthy=True)

    async def generate(self, request: VideoRequest) -> VideoArtifact:
        raise RuntimeError("model exploded mid-render")


class WrongModePlugin:
    """A plugin that returns the wrong mode — a conformance failure."""

    capabilities = _T2V_PROFILE

    def __init__(self, *, config: dict[str, Any], host: object) -> None: ...

    async def probe(self) -> ProbeResult:
        return ProbeResult(healthy=True)

    async def generate(self, request: VideoRequest) -> VideoArtifact:
        return VideoArtifact(
            clip_url="https://example.invalid/clip.mp4",
            duration_s=5.0,
            model="wrong.model",
            mode=RenderMode.IMAGE_TO_VIDEO,  # mismatch vs requested t2v
        )


def make_manifest_dict(
    *,
    plugin_id: str = "com.acme.good",
    version: str = "1.0.0",
    kinora_api: str = f">={PLUGIN_API_VERSION},<2.0.0",
    entry_point: str = "acme_plugin:create",
    modes: tuple[str, ...] = ("text_to_video",),
    config_schema: list[dict[str, Any]] | None = None,
    resource_limits: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a valid manifest dict (override fields via kwargs)."""
    data: dict[str, Any] = {
        "id": plugin_id,
        "version": version,
        "name": "Acme Video",
        "kinora_api": kinora_api,
        "entry_point": entry_point,
        # Mirror ``_T2V_PROFILE`` exactly so the manifest profile equals the
        # runtime ``GoodPlugin.capabilities`` (the ``capabilities_match`` gate).
        "capabilities": {
            "modes": list(modes),
            "resolutions": ["720P"],
            "min_duration_s": 1.0,
            "max_duration_s": 10.0,
            "supports_negative_prompt": True,
            "supports_seed": True,
        },
    }
    if config_schema is not None:
        data["config_schema"] = config_schema
    if resource_limits is not None:
        data["resource_limits"] = resource_limits
    data.update(extra)
    return data


def make_manifest(**kwargs: Any) -> PluginManifest:
    return PluginManifest.parse(make_manifest_dict(**kwargs))


def resolver_for(factory: Callable[..., VideoProviderPlugin]) -> Callable[[str, str], Any]:
    """An entry-point resolver that always returns ``factory`` (no real import)."""

    def _resolve(_module: str, _attr: str) -> Callable[..., VideoProviderPlugin]:
        return factory

    return _resolve


@pytest.fixture
def good_manifest() -> PluginManifest:
    return make_manifest()


@pytest.fixture
def good_factory() -> Callable[..., VideoProviderPlugin]:
    return GoodPlugin
