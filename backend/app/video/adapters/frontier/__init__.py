"""Frontier hosted video-provider adapters.

Concrete adapters for the major frontier hosted text/image-to-video models —
**Runway** (Gen-3/Gen-4), **Luma** Dream Machine, **Pika**, **Kling**, Google
**Veo**, and OpenAI **Sora** — behind one ``UniversalVideoProvider`` interface.

Each adapter:

* declares its real capability profile (modes, durations, resolutions, aspect ratios,
  fps, seed / negative-prompt / reference-image support) as a
  :class:`~app.video.adapters.frontier.types.CapabilityProfile`;
* maps Kinora's canonical request (a :class:`~app.video.adapters.frontier.types.FrontierRequest`,
  itself derived from a :class:`~app.providers.types.WanSpec`) onto the provider's
  native submit body;
* drives the async job lifecycle ``submit`` → poll-to-completion → ``fetch`` and
  **eagerly downloads** the produced clip bytes (provider asset URLs expire),
  returning ``clip_bytes`` + ``last_frame_bytes`` + ``duration`` in a
  :class:`~app.providers.types.VideoResult`;
* maps provider-native error codes into the shared
  :class:`~app.video.adapters.frontier.errors.FrontierError` taxonomy (which subclasses
  the in-repo provider errors, so an adapter is a drop-in
  :class:`~app.providers.video_router.VideoBackend`);
* handles per-provider quirks (aspect-ratio/ratio tokens, prompt-length caps,
  nested business-code responses, base64 vs data-URI images, content-sub-resource
  downloads).

Real network calls go through a thin
:class:`~app.video.adapters.frontier.transport.FrontierTransport` behind
``settings.frontier_video_enabled`` (default **OFF**, on top of the global
``KINORA_LIVE_VIDEO`` spend gate). Tests inject a deterministic fake transport.
"""

from __future__ import annotations

from .base import BaseFrontierAdapter
from .errors import (
    FrontierAuthError,
    FrontierBadRequest,
    FrontierBadResponse,
    FrontierContentModerated,
    FrontierError,
    FrontierErrorCode,
    FrontierJobCanceled,
    FrontierJobFailed,
    FrontierQuotaExhausted,
    FrontierRateLimited,
    FrontierServerError,
    FrontierTimeout,
    FrontierUnsupportedCapability,
    build_error,
    code_for_status,
)
from .kling import KlingAdapter, build_kling_adapter
from .luma import LumaAdapter, build_luma_adapter
from .pika import PikaAdapter, build_pika_adapter
from .registry import (
    FRONTIER_BUILDERS,
    adapters_supporting,
    available_slugs,
    build_adapter,
    build_configured_adapters,
    capability_catalog,
    configured_slugs,
)
from .runway import RunwayAdapter, build_runway_adapter
from .sora import SoraAdapter, build_sora_adapter
from .transport import (
    FrontierRetryConfig,
    FrontierTransport,
    FrontierTransportDisabled,
)
from .types import (
    CapabilityProfile,
    FetchedClip,
    FrontierRequest,
    JobStatus,
    PollResult,
    SubmitHandle,
    UniversalVideoProvider,
    VideoMode,
    from_wan_spec,
    mode_from_wan,
    mode_to_wan,
    supported_modes_summary,
    validate_against_profile,
)
from .veo import VeoAdapter, build_veo_adapter

__all__ = [
    # registry
    "FRONTIER_BUILDERS",
    "adapters_supporting",
    "available_slugs",
    "build_adapter",
    "build_configured_adapters",
    "capability_catalog",
    "configured_slugs",
    # base + protocol + types
    "BaseFrontierAdapter",
    "CapabilityProfile",
    "FetchedClip",
    "FrontierRequest",
    "JobStatus",
    "PollResult",
    "SubmitHandle",
    "UniversalVideoProvider",
    "VideoMode",
    "from_wan_spec",
    "mode_from_wan",
    "mode_to_wan",
    "supported_modes_summary",
    "validate_against_profile",
    # transport
    "FrontierRetryConfig",
    "FrontierTransport",
    "FrontierTransportDisabled",
    # errors
    "FrontierAuthError",
    "FrontierBadRequest",
    "FrontierBadResponse",
    "FrontierContentModerated",
    "FrontierError",
    "FrontierErrorCode",
    "FrontierJobCanceled",
    "FrontierJobFailed",
    "FrontierQuotaExhausted",
    "FrontierRateLimited",
    "FrontierServerError",
    "FrontierTimeout",
    "FrontierUnsupportedCapability",
    "build_error",
    "code_for_status",
    # adapters
    "KlingAdapter",
    "LumaAdapter",
    "PikaAdapter",
    "RunwayAdapter",
    "SoraAdapter",
    "VeoAdapter",
    "build_kling_adapter",
    "build_luma_adapter",
    "build_pika_adapter",
    "build_runway_adapter",
    "build_sora_adapter",
    "build_veo_adapter",
]
