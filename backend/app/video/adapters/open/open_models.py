"""Open-weights model adapters: Genmo Mochi, CogVideoX, LTX-Video, HunyuanVideo.

These are open-weights diffusion video models. In production Kinora runs them on a
gateway (Replicate / fal) or a self-hosted box; what is *model-specific* is purely
their capability envelope and their native ``input`` field names — exactly what the
:class:`~.replicate.ReplicateProvider` / :class:`~.fal.FalProvider` meta-adapters
are parameterised by.

So each model here is expressed as (a) a canonical :func:`Capabilities` profile and
(b) a :class:`~.replicate.InputMap`, with a thin ``build()`` that wires both onto a
gateway. This keeps every model a *named, first-class* backend (``MochiProvider``,
``CogVideoXProvider``, ...) — discoverable, capability-declaring, individually
testable — while reusing the audited submit/poll/fetch/last-frame lifecycle rather
than duplicating it five times.

A self-hosted deployment of any of these is instead a one-file *descriptor* (see
``descriptors/``), proving the same model can be onboarded with zero code.
"""

from __future__ import annotations

from typing import Any

from app.providers.types import WanMode

from .base import PollConfig
from .fal import FalProvider
from .interface import Capabilities
from .replicate import InputMap, ReplicateProvider

__all__ = [
    "CogVideoXProvider",
    "HunyuanVideoProvider",
    "LTXVideoProvider",
    "MochiProvider",
    "cogvideox_capabilities",
    "hunyuan_capabilities",
    "ltx_capabilities",
    "mochi_capabilities",
]


# --------------------------------------------------------------------------- #
# Capability profiles (canonical, declarative)
# --------------------------------------------------------------------------- #


def mochi_capabilities(name: str = "genmo-mochi") -> Capabilities:
    """Genmo **Mochi 1** — high-fidelity text-to-video, ~5.4s at 30fps, 480p."""
    return Capabilities(
        name=name,
        modes=frozenset({WanMode.TEXT_TO_VIDEO}),
        max_duration_s=5.4,
        min_duration_s=1.0,
        resolutions=frozenset({"480P", "848x480"}),
        supports_seed=True,
        supports_negative_prompt=True,
        supports_audio=False,
        default_fps=30,
        cost_per_s=0.7,
        quality=0.7,
    )


def cogvideox_capabilities(name: str = "cogvideox") -> Capabilities:
    """**CogVideoX-5B** — text- and image-to-video, ~6s at 8fps, 720x480."""
    return Capabilities(
        name=name,
        modes=frozenset({WanMode.TEXT_TO_VIDEO, WanMode.IMAGE_TO_VIDEO}),
        max_duration_s=6.0,
        min_duration_s=1.0,
        resolutions=frozenset({"480P", "720P", "720x480"}),
        supports_seed=True,
        supports_negative_prompt=True,
        supports_audio=False,
        default_fps=8,
        cost_per_s=0.5,
        quality=0.62,
    )


def ltx_capabilities(name: str = "ltx-video") -> Capabilities:
    """Lightricks **LTX-Video** — fast t2v + i2v, up to ~8s, real-time class."""
    return Capabilities(
        name=name,
        modes=frozenset({WanMode.TEXT_TO_VIDEO, WanMode.IMAGE_TO_VIDEO, WanMode.FIRST_LAST_FRAME}),
        max_duration_s=8.0,
        min_duration_s=1.0,
        resolutions=frozenset({"480P", "720P", "768x512"}),
        supports_seed=True,
        supports_negative_prompt=True,
        supports_audio=False,
        default_fps=24,
        cost_per_s=0.3,
        quality=0.6,
    )


def hunyuan_capabilities(name: str = "hunyuan-video") -> Capabilities:
    """Tencent **HunyuanVideo** — large t2v + i2v, ~5s, 720p, top open quality."""
    return Capabilities(
        name=name,
        modes=frozenset({WanMode.TEXT_TO_VIDEO, WanMode.IMAGE_TO_VIDEO}),
        max_duration_s=5.0,
        min_duration_s=1.0,
        resolutions=frozenset({"480P", "720P", "1280x720"}),
        supports_seed=True,
        supports_negative_prompt=True,
        supports_audio=False,
        default_fps=24,
        cost_per_s=0.8,
        quality=0.78,
    )


# --------------------------------------------------------------------------- #
# Named providers (each = a capability profile + an InputMap over a gateway)
# --------------------------------------------------------------------------- #


class MochiProvider(ReplicateProvider):
    """Genmo Mochi 1, hosted on Replicate (text-to-video)."""

    @classmethod
    def build(  # type: ignore[override]
        cls,
        *,
        version: str,
        api_key: str | None,
        allow_network: bool,
        live_video: bool,
        name: str = "genmo-mochi",
        poll: PollConfig | None = None,
        usage_sink: Any | None = None,
        transport: object | None = None,
        settings: Any | None = None,
    ) -> MochiProvider:
        return cls(
            version=version,
            capabilities=mochi_capabilities(name),
            transport=_replicate_transport(api_key, allow_network, transport, settings),
            live_video=live_video,
            input_map=InputMap(
                prompt="prompt",
                negative_prompt="negative_prompt",
                seed="seed",
                num_frames="num_frames",
            ),
            poll=poll,
            name=name,
            usage_sink=usage_sink,
            model_label=name,
        )


class CogVideoXProvider(ReplicateProvider):
    """CogVideoX-5B (t2v + i2v) on Replicate."""

    @classmethod
    def build(  # type: ignore[override]
        cls,
        *,
        version: str,
        api_key: str | None,
        allow_network: bool,
        live_video: bool,
        name: str = "cogvideox",
        poll: PollConfig | None = None,
        usage_sink: Any | None = None,
        transport: object | None = None,
        settings: Any | None = None,
    ) -> CogVideoXProvider:
        return cls(
            version=version,
            capabilities=cogvideox_capabilities(name),
            transport=_replicate_transport(api_key, allow_network, transport, settings),
            live_video=live_video,
            input_map=InputMap(
                prompt="prompt",
                negative_prompt="negative_prompt",
                seed="seed",
                image="image",
                num_frames="num_frames",
            ),
            poll=poll,
            name=name,
            usage_sink=usage_sink,
            model_label=name,
        )


class LTXVideoProvider(ReplicateProvider):
    """Lightricks LTX-Video (t2v + i2v + first-last-frame) on Replicate."""

    @classmethod
    def build(  # type: ignore[override]
        cls,
        *,
        version: str,
        api_key: str | None,
        allow_network: bool,
        live_video: bool,
        name: str = "ltx-video",
        poll: PollConfig | None = None,
        usage_sink: Any | None = None,
        transport: object | None = None,
        settings: Any | None = None,
    ) -> LTXVideoProvider:
        return cls(
            version=version,
            capabilities=ltx_capabilities(name),
            transport=_replicate_transport(api_key, allow_network, transport, settings),
            live_video=live_video,
            input_map=InputMap(
                prompt="prompt",
                negative_prompt="negative_prompt",
                seed="seed",
                image="image",
                first_frame="image",
                last_frame="target_image",
                num_frames="num_frames",
                fps="fps",
            ),
            poll=poll,
            name=name,
            usage_sink=usage_sink,
            model_label=name,
        )


class HunyuanVideoProvider(FalProvider):
    """Tencent HunyuanVideo (t2v + i2v) on fal.ai."""

    @classmethod
    def build(  # type: ignore[override]
        cls,
        *,
        app_id: str = "fal-ai/hunyuan-video",
        api_key: str | None,
        allow_network: bool,
        live_video: bool,
        name: str = "hunyuan-video",
        poll: PollConfig | None = None,
        usage_sink: Any | None = None,
        transport: object | None = None,
        settings: Any | None = None,
    ) -> HunyuanVideoProvider:
        from .transport import OpenHttpTransport, OpenTransportConfig

        cfg = OpenTransportConfig(
            base_url="https://queue.fal.run",
            api_key=api_key,
            auth_scheme="key",
            allow_network=allow_network,
        )
        http = OpenHttpTransport(cfg, transport=transport, settings=settings)  # type: ignore[arg-type]
        return cls(
            app_id=app_id,
            capabilities=hunyuan_capabilities(name),
            transport=http,
            live_video=live_video,
            input_map=InputMap(
                prompt="prompt",
                negative_prompt="negative_prompt",
                seed="seed",
                image="image_url",
                num_frames="num_frames",
            ),
            poll=poll,
            name=name,
            usage_sink=usage_sink,
        )


def _replicate_transport(
    api_key: str | None,
    allow_network: bool,
    transport: object | None,
    settings: Any | None = None,
) -> Any:
    from .transport import OpenHttpTransport, OpenTransportConfig

    cfg = OpenTransportConfig(
        base_url="https://api.replicate.com/v1",
        api_key=api_key,
        auth_scheme="token",
        allow_network=allow_network,
    )
    return OpenHttpTransport(cfg, transport=transport, settings=settings)  # type: ignore[arg-type]
