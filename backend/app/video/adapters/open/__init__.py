"""Open / self-hosted / gateway video model adapters + the config-only descriptor.

This leaf subpackage owns the real exports (the shared parent ``__init__`` files
are intentionally empty one-liners). It provides:

* a router-compatible :class:`OpenVideoBackend` interface (mirrors
  :class:`app.providers.video_router.VideoBackend`) + :class:`Capabilities`;
* the shared :class:`BaseOpenAdapter` submit→poll→fetch→last-frame lifecycle
  behind a default-OFF network gate, honouring ``KINORA_LIVE_VIDEO``;
* concrete open-model adapters — Stability SVD, Genmo Mochi, CogVideoX,
  Lightricks LTX-Video, Tencent HunyuanVideo;
* the meta-adapter family — :class:`ReplicateProvider`, :class:`FalProvider`, and
  the descriptor-driven :class:`DescriptorAdapter` (aliased
  :class:`ComfyUIProvider` / :class:`OpenAPIProvider`);
* the **provider descriptor** format (:class:`ProviderDescriptor`) so a brand-new
  model is onboarded by configuration alone — no new code — plus a
  :func:`build_adapter` registry over all of the above.
"""

from __future__ import annotations

from .base import BaseOpenAdapter, PollConfig
from .descriptor import (
    CapabilitiesSpec,
    PollSpec,
    ProviderDescriptor,
    SubmitSpec,
    TransportSpec,
    load_descriptor,
)
from .descriptor_adapter import (
    ComfyUIProvider,
    DescriptorAdapter,
    OpenAPIProvider,
    build_from_descriptor,
)
from .fal import FalProvider
from .interface import (
    Capabilities,
    OpenVideoBackend,
    SubmitPollFetch,
    SubmittedTask,
    TaskStatus,
    select_backend,
)
from .jsonpath import jsonpath_all, jsonpath_first, select
from .lastframe import extract_last_frame, ffmpeg_available
from .open_models import (
    CogVideoXProvider,
    HunyuanVideoProvider,
    LTXVideoProvider,
    MochiProvider,
    cogvideox_capabilities,
    hunyuan_capabilities,
    ltx_capabilities,
    mochi_capabilities,
)
from .registry import (
    OpenAdapterSpec,
    UnknownAdapterKind,
    build_adapter,
    build_fleet,
    bundled_descriptor_paths,
    load_bundled,
)
from .replicate import InputMap, ReplicateProvider
from .stability_svd import StableVideoDiffusionProvider, svd_capabilities
from .template import build_context, render_template
from .transport import NetworkDisabled, OpenHttpTransport, OpenTransportConfig

__all__ = [
    # interface
    "Capabilities",
    "OpenVideoBackend",
    "SubmitPollFetch",
    "SubmittedTask",
    "TaskStatus",
    "select_backend",
    # base / transport
    "BaseOpenAdapter",
    "PollConfig",
    "NetworkDisabled",
    "OpenHttpTransport",
    "OpenTransportConfig",
    # last-frame
    "extract_last_frame",
    "ffmpeg_available",
    # descriptor + engine
    "CapabilitiesSpec",
    "ComfyUIProvider",
    "DescriptorAdapter",
    "OpenAPIProvider",
    "PollSpec",
    "ProviderDescriptor",
    "SubmitSpec",
    "TransportSpec",
    "build_context",
    "build_from_descriptor",
    "load_descriptor",
    "render_template",
    # jsonpath
    "jsonpath_all",
    "jsonpath_first",
    "select",
    # gateways
    "FalProvider",
    "InputMap",
    "ReplicateProvider",
    # open models
    "CogVideoXProvider",
    "HunyuanVideoProvider",
    "LTXVideoProvider",
    "MochiProvider",
    "StableVideoDiffusionProvider",
    "cogvideox_capabilities",
    "hunyuan_capabilities",
    "ltx_capabilities",
    "mochi_capabilities",
    "svd_capabilities",
    # registry
    "OpenAdapterSpec",
    "UnknownAdapterKind",
    "build_adapter",
    "build_fleet",
    "bundled_descriptor_paths",
    "load_bundled",
]
