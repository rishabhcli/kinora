"""Universal Video Provider abstraction — the stable, provider-agnostic seam.

Kinora's render path is built around a specific model family (hosted Wan via
DashScope, §9.3). This package is the contract that lets Kinora drive **any**
video-gen model behind one interface, so a second hosted region, a self-hosted
lane, MiniMax, or a future model all plug in identically:

* :class:`VideoCapability` (+ :class:`CapabilityQuery`) — each provider declares
  its envelope (modes, duration window, resolutions/aspect/fps, seed/negative
  support, reference style, audio, prompt length, async-vs-sync) and answers a
  structured "can you do r2v at 720p for 5s?" query.
* :class:`CanonicalVideoRequest` / :class:`CanonicalVideoResult` — a
  provider-neutral request/result the Wan types map to/from losslessly.
* :class:`UniversalVideoProvider` (Protocol) / :class:`BaseVideoProvider` (ABC) —
  the ``capabilities/submit/poll/fetch/cancel`` lifecycle every adapter implements.
* :class:`ProviderRegistry` — register/look up providers by id or capability query
  with deterministic, optionally cost-/quality-ranked selection.
* :class:`Normalizer` — translates canonical ↔ :class:`WanSpec` and canonical ↔
  provider-native dicts.
* :class:`EchoVideoProvider` — the deterministic, network-free reference adapter.

Nothing here spends video-seconds or touches the network; the real spend gate
(``KINORA_LIVE_VIDEO``) stays owned by the concrete hosted provider.
"""

from __future__ import annotations

from .capability import (
    CapabilityQuery,
    ReferenceStyle,
    SubmitStyle,
    VideoCapability,
    VideoMode,
    normalize_aspect,
    normalize_resolution,
)
from .echo import EchoVideoProvider, default_echo_capability
from .normalizer import Normalizer, canonical_mode_to_wan, wan_mode_to_canonical
from .provider import (
    BaseVideoProvider,
    UniversalVideoProvider,
    VideoProviderError,
    VideoRenderTimeout,
)
from .registry import (
    DuplicateProvider,
    ProviderNotFound,
    ProviderRanking,
    ProviderRegistry,
    SelectionStrategy,
)
from .schema import (
    CanonicalVideoRequest,
    CanonicalVideoResult,
    MediaRef,
    MediaRole,
    TaskState,
    VideoTaskHandle,
)

__all__ = [
    "BaseVideoProvider",
    "CanonicalVideoRequest",
    "CanonicalVideoResult",
    "CapabilityQuery",
    "DuplicateProvider",
    "EchoVideoProvider",
    "MediaRef",
    "MediaRole",
    "Normalizer",
    "ProviderNotFound",
    "ProviderRanking",
    "ProviderRegistry",
    "ReferenceStyle",
    "SelectionStrategy",
    "SubmitStyle",
    "TaskState",
    "UniversalVideoProvider",
    "VideoCapability",
    "VideoMode",
    "VideoProviderError",
    "VideoRenderTimeout",
    "VideoTaskHandle",
    "canonical_mode_to_wan",
    "default_echo_capability",
    "normalize_aspect",
    "normalize_resolution",
    "wan_mode_to_canonical",
]
