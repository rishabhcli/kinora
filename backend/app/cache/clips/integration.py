"""Composition helpers — wire a :class:`RenderCache` from app settings + infra.

Strictly additive: nothing here edits ``app.core.config`` or
``app.composition``. A composition root calls :func:`build_render_cache` with the
pieces it already owns (an object store, an optional binary-mode Redis client) and
holds the result. The default ``provider`` is read from ``Settings.video_backend``
and the ``model`` from the matching model id, so a :class:`RenderInputs` built via
:meth:`RenderInputs.from_spec` lines up with the provider the pipeline will use.
"""

from __future__ import annotations

from typing import Any

from app.cache.clips.dedup import CLIP_NAMESPACE, DEFAULT_CLIP_TTL_S, RenderCache
from app.cache.clips.keys import RenderInputs
from app.cache.clips.store import ClipBlobStore
from app.cache.clock import SYSTEM_CLOCK, Clock
from app.cache.metrics import CacheMetrics


def provider_and_model(settings: Any) -> tuple[str, str]:
    """Resolve the active video provider + default i2v/r2v model from settings.

    Mirrors the pipeline's provider selection (``video_backend`` ``"dashscope"`` |
    ``"minimax"``). The reference-to-video model is the render path's default, so
    a content-addressed key built from a spec matches what gets rendered.
    """
    backend = str(getattr(settings, "video_backend", "dashscope")).strip().casefold()
    if backend == "minimax":
        return "minimax", str(getattr(settings, "minimax_video_model", ""))
    return "dashscope", str(getattr(settings, "video_model_r2v", ""))


def render_inputs_from_spec(spec: Any, settings: Any, **overrides: Any) -> RenderInputs:
    """Build :class:`RenderInputs` from a shot spec using the settings' provider.

    Convenience for call sites that have a spec + settings but shouldn't have to
    know the provider/model resolution. Keyword ``overrides`` (e.g.
    ``resolution=...``, ``duration_s=...``) are passed through to
    :meth:`RenderInputs.from_spec`.
    """
    provider, model = provider_and_model(settings)
    resolution = overrides.pop("resolution", None)
    if resolution is None and provider == "minimax":
        resolution = str(getattr(settings, "minimax_resolution", "")) or None
    return RenderInputs.from_spec(
        spec, provider=provider, model=model, resolution=resolution, **overrides
    )


def build_render_cache(
    *,
    object_store: ClipBlobStore | None = None,
    redis: object | None = None,
    namespace: str = CLIP_NAMESPACE,
    clock: Clock | None = None,
    metrics: CacheMetrics | None = None,
    l1_max_entries: int = 2048,
    url_ttl: int = 3600,
    record_ttl_s: float | None = DEFAULT_CLIP_TTL_S,
) -> RenderCache:
    """Build the production :class:`RenderCache` over the supplied tiers.

    Pass the object store to enable the durable L3 tier (fleet-wide reuse) and a
    binary-mode async Redis client to enable the cross-process L2 tier. With
    neither, the cache is in-process only (still useful within one worker).
    """
    return RenderCache.build(
        namespace=namespace,
        clock=clock or SYSTEM_CLOCK,
        metrics=metrics,
        l1_max_entries=l1_max_entries,
        redis=redis,
        object_store=object_store,
        url_ttl=url_ttl,
        record_ttl_s=record_ttl_s,
    )


def build_render_cache_from_settings(
    settings: Any,
    *,
    object_store: ClipBlobStore | None = None,
    redis: object | None = None,
    metrics: CacheMetrics | None = None,
    clock: Clock | None = None,
) -> RenderCache:
    """Build a :class:`RenderCache` reading L1 size / URL TTL from ``settings``.

    Falls back to module defaults when a setting is absent, so it works with the
    real :class:`app.core.config.Settings` and with light test doubles alike.
    """
    return build_render_cache(
        object_store=object_store,
        redis=redis,
        clock=clock,
        metrics=metrics,
        l1_max_entries=int(getattr(settings, "clip_cache_l1_max_entries", 2048)),
        url_ttl=int(getattr(settings, "clip_cache_url_ttl_s", 3600)),
        record_ttl_s=getattr(settings, "clip_cache_record_ttl_s", DEFAULT_CLIP_TTL_S),
    )


__all__ = [
    "build_render_cache",
    "build_render_cache_from_settings",
    "provider_and_model",
    "render_inputs_from_spec",
]
