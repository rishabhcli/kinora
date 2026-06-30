"""A small registry/factory for the frontier adapters.

Maps a provider slug → its builder, so the composition root (or a router) can spin up
exactly the adapters that are configured, inspect their capability profiles, and pick
a provider for a given mode. Pure of network: building an adapter never calls out.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from app.core.config import Settings

from .base import BaseFrontierAdapter
from .kling import build_kling_adapter
from .luma import build_luma_adapter
from .pika import build_pika_adapter
from .runway import build_runway_adapter
from .sora import build_sora_adapter
from .types import CapabilityProfile, VideoMode
from .veo import build_veo_adapter

#: A builder takes Settings (+ optional injected transport / kwargs) → an adapter.
AdapterBuilder = Callable[..., BaseFrontierAdapter]

#: Every known frontier provider slug → its builder.
FRONTIER_BUILDERS: dict[str, AdapterBuilder] = {
    "runway": build_runway_adapter,
    "luma": build_luma_adapter,
    "pika": build_pika_adapter,
    "kling": build_kling_adapter,
    "veo": build_veo_adapter,
    "sora": build_sora_adapter,
}

#: Which Settings key holds each provider's API key (used by :func:`configured_slugs`).
_KEY_ATTR: dict[str, str] = {
    "runway": "runway_api_key",
    "luma": "luma_api_key",
    "pika": "pika_api_key",
    "kling": "kling_api_key",
    "veo": "veo_api_key",
    "sora": "sora_api_key",
}


def available_slugs() -> list[str]:
    """All known frontier provider slugs (sorted)."""
    return sorted(FRONTIER_BUILDERS)


def configured_slugs(settings: Settings) -> list[str]:
    """Slugs whose API key is present *and* the frontier transport flag is on."""
    if not settings.frontier_video_enabled:
        return []
    return sorted(slug for slug, attr in _KEY_ATTR.items() if getattr(settings, attr, None))


def build_adapter(slug: str, settings: Settings, **kwargs: object) -> BaseFrontierAdapter:
    """Build a single adapter by slug. Raises ``KeyError`` for an unknown slug."""
    builder = FRONTIER_BUILDERS[slug]
    return builder(settings, **kwargs)


def build_configured_adapters(settings: Settings) -> list[BaseFrontierAdapter]:
    """Build every adapter that is configured (key present + flag on)."""
    return [build_adapter(slug, settings) for slug in configured_slugs(settings)]


def capability_catalog(settings: Settings) -> dict[str, CapabilityProfile]:
    """A {slug → CapabilityProfile} map across *all* known providers (no network).

    Uses each provider's default model id; safe to call without keys (capability
    inspection does not submit anything).
    """
    return {slug: build_adapter(slug, settings).capabilities() for slug in available_slugs()}


def adapters_supporting(
    mode: VideoMode,
    adapters: Sequence[BaseFrontierAdapter],
) -> list[BaseFrontierAdapter]:
    """Filter adapters to those whose capability profile supports ``mode``."""
    return [a for a in adapters if a.capabilities().supports_mode(mode)]


__all__ = [
    "FRONTIER_BUILDERS",
    "AdapterBuilder",
    "adapters_supporting",
    "available_slugs",
    "build_adapter",
    "build_configured_adapters",
    "capability_catalog",
    "configured_slugs",
]
