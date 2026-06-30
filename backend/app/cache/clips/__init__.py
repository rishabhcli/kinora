"""Content-addressed render-clip caching & dedup layer (kinora.md §8.7 / §11.1).

Rendering a clip is Kinora's single most expensive operation. This subpackage
avoids paying twice for the same render:

* :mod:`~app.cache.clips.keys` — a deterministic **content-addressed key**
  derived purely from the render-determining inputs (prompt + camera + seed +
  mode + reference-identity digest + provider + model + duration), with explicit
  normalisation so semantically-identical requests collide **across books and
  sessions** (unlike the book-scoped §8.7 ``shot_hash``).
* :mod:`~app.cache.clips.record` — the typed :class:`ClipRecord` a hit serves
  (object-store pointer + provenance + the video-seconds it saved) and
  :class:`ClipLookup`.
* :mod:`~app.cache.clips.store` — the durable **object-store cache tier**
  (:class:`ObjectStoreCacheBackend`) and its test fake (:class:`InMemoryClipStore`).
* :mod:`~app.cache.clips.tiers` — assembles the **L1 -> L2 -> L3** tier stack
  (in-process LRU -> Redis -> object store) on the generic cache mechanism.
* :mod:`~app.cache.clips.dedup` — :class:`RenderCache`, the public facade:
  typed get/put/invalidate, **singleflight / request-coalescing** so concurrent
  identical renders run once and share the result, cross-book reuse accounting,
  a warmup/prefetch hook for the scheduler, and stats (hit/miss/eviction +
  ``video_seconds_saved``).
* :mod:`~app.cache.clips.integration` — additive composition helpers
  (``build_render_cache[_from_settings]``).

This layer is complementary to :mod:`app.memory.cache_service` (the DB-backed,
book-scoped §8.7 idempotency cache) and to the general :mod:`app.cache` (which it
builds on); it neither edits nor replaces either.
"""

from __future__ import annotations

from app.cache.clips.dedup import CLIP_NAMESPACE, DEFAULT_CLIP_TTL_S, RenderCache
from app.cache.clips.integration import (
    build_render_cache,
    build_render_cache_from_settings,
    provider_and_model,
    render_inputs_from_spec,
)
from app.cache.clips.keys import (
    RENDER_KEY_SCHEMA,
    RenderInputs,
    RenderKey,
    normalize_camera,
    normalize_text,
    quantize_duration,
    reference_identity_digest,
    render_key,
)
from app.cache.clips.record import ClipLookup, ClipRecord
from app.cache.clips.store import (
    SIDECAR_PREFIX,
    ClipBlobStore,
    InMemoryClipStore,
    ObjectStoreCacheBackend,
)
from app.cache.clips.tiers import build_clip_backend, build_clip_cache

__all__ = [
    "CLIP_NAMESPACE",
    "DEFAULT_CLIP_TTL_S",
    "RENDER_KEY_SCHEMA",
    "SIDECAR_PREFIX",
    "ClipBlobStore",
    "ClipLookup",
    "ClipRecord",
    "InMemoryClipStore",
    "ObjectStoreCacheBackend",
    "RenderCache",
    "RenderInputs",
    "RenderKey",
    "build_clip_backend",
    "build_clip_cache",
    "build_render_cache",
    "build_render_cache_from_settings",
    "normalize_camera",
    "normalize_text",
    "provider_and_model",
    "quantize_duration",
    "reference_identity_digest",
    "render_inputs_from_spec",
    "render_key",
]
