"""Config-driven video-provider catalog + runtime registry + introspection API.

This subsystem is the **single declarative source of truth** for which video
models Kinora can route to and what each can do. It is purely additive and never
renders or spends (``KINORA_LIVE_VIDEO`` is irrelevant here):

* :mod:`~app.video.registry.capabilities` — a small, *self-owned* capability
  vocabulary (:class:`VideoMode` / :class:`Resolution` / :class:`CapabilityProfile`)
  so the catalog reasons about "can serve mode=r2v, 720P, ≥8s?" without depending
  on a sibling's ``WanMode``/``WanSpec``.
* :mod:`~app.video.registry.catalog` — the declarative data model
  (:class:`ProviderEntry` / :class:`ProviderCatalog`) and a YAML/JSON loader,
  plus the checked-in :file:`providers.yaml` baseline.
* :mod:`~app.video.registry.picker` — a deterministic, hash-based weighted picker
  for canary / A-B routing (no global RNG; stable per routing key).
* :mod:`~app.video.registry.registry` — the runtime
  :class:`VideoProviderRegistry`: register/unregister, lookup, capability
  queries, per-provider feature flags + weight overrides, deterministic
  weighted selection, and a safe (validate-before-swap) hot-reload.
* :mod:`~app.video.registry.api` — a read-only FastAPI introspection router
  (``/video/providers``, ``/video/providers/{id}``, ``/video/capabilities``).
"""

from __future__ import annotations

from app.video.registry.capabilities import (
    CapabilityProfile,
    Resolution,
    VideoMode,
)
from app.video.registry.catalog import (
    CatalogError,
    ProviderCatalog,
    ProviderEntry,
    ProviderKind,
    RolloutState,
    load_catalog_file,
    load_catalog_text,
    load_default_catalog,
)
from app.video.registry.picker import (
    WeightedCandidate,
    expected_distribution,
    pick_weighted,
)
from app.video.registry.registry import VideoProviderRegistry, register_runtime

__all__ = [
    "CapabilityProfile",
    "CatalogError",
    "ProviderCatalog",
    "ProviderEntry",
    "ProviderKind",
    "Resolution",
    "RolloutState",
    "VideoMode",
    "VideoProviderRegistry",
    "WeightedCandidate",
    "expected_distribution",
    "load_catalog_file",
    "load_catalog_text",
    "load_default_catalog",
    "pick_weighted",
    "register_runtime",
]
