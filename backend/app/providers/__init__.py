"""Real DashScope (Qwen Cloud) provider layer for Kinora.

Production integrations — chat, vision-language, image (gen + edit), TTS
(voice clone + narration), and Wan 2.7 video — over a shared resilient client
(:class:`ProviderClient`) with retries, a circuit breaker, a rate limiter, and a
cost-accounting sink. No mocks: every provider calls the live API. Real Wan video
renders are gated behind ``settings.kinora_live_video``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.config import Settings, get_settings

if TYPE_CHECKING:
    from .local_wan import LocalWanVideoProvider

from .base import (
    BreakerState,
    CircuitBreaker,
    LoggingUsageSink,
    ProviderClient,
    ResilienceConfig,
    TokenBucket,
    UsageSink,
    classify_status,
    data_uri,
    sdk_get,
)
from .chat import ChatProvider
from .embeddings import EMBED_DIM, EmbeddingProvider, cosine
from .errors import (
    AuthenticationError,
    CircuitOpenError,
    LiveVideoDisabled,
    ModelNotAvailable,
    ProviderBadRequest,
    ProviderError,
    ProviderTimeout,
    RateLimited,
    ResponseParseError,
    TransientProviderError,
)
from .image import ImageProvider
from .tts import TtsProvider
from .types import (
    ChatResult,
    ToolCall,
    TtsResult,
    TtsWord,
    Usage,
    UsageTotals,
    VideoResult,
    WanMode,
    WanSpec,
)
from .video import VideoPollConfig, VideoProvider
from .vl import VLProvider


@dataclass(frozen=True, slots=True)
class Providers:
    """All providers wired to one shared :class:`ProviderClient`.

    The single client means one rate limiter, one circuit breaker, and one cost
    sink across the whole agent crew — the budget service subscribes to that one
    sink.
    """

    client: ProviderClient
    chat: ChatProvider
    vl: VLProvider
    image: ImageProvider
    tts: TtsProvider
    video: VideoProvider | LocalWanVideoProvider
    embeddings: EmbeddingProvider

    async def aclose(self) -> None:
        await self.client.aclose()


def create_providers(
    settings: Settings | None = None,
    *,
    usage_sink: UsageSink | None = None,
    resilience: ResilienceConfig | None = None,
) -> Providers:
    """Construct a shared client and all providers bound to it."""
    resolved = settings or get_settings()
    client = ProviderClient(
        resolved,
        usage_sink=usage_sink,
        resilience=resilience,
    )
    # TEMPORARY: route video to the local Wan2.2 host server when selected;
    # default ("cloud") keeps the DashScope Wan provider unchanged.
    video: VideoProvider | LocalWanVideoProvider
    if resolved.video_backend == "local":
        from .local_wan import LocalWanVideoProvider

        video = LocalWanVideoProvider(resolved)
    else:
        video = VideoProvider(client)
    return Providers(
        client=client,
        chat=ChatProvider(client),
        vl=VLProvider(client),
        image=ImageProvider(client),
        tts=TtsProvider(client),
        video=video,
        embeddings=EmbeddingProvider(client),
    )


__all__ = [
    "EMBED_DIM",
    "AuthenticationError",
    "BreakerState",
    "ChatProvider",
    "ChatResult",
    "CircuitBreaker",
    "CircuitOpenError",
    "EmbeddingProvider",
    "ImageProvider",
    "LiveVideoDisabled",
    "LoggingUsageSink",
    "ModelNotAvailable",
    "ProviderBadRequest",
    "ProviderClient",
    "ProviderError",
    "ProviderTimeout",
    "Providers",
    "RateLimited",
    "ResilienceConfig",
    "ResponseParseError",
    "TokenBucket",
    "ToolCall",
    "TransientProviderError",
    "TtsProvider",
    "TtsResult",
    "TtsWord",
    "Usage",
    "UsageSink",
    "UsageTotals",
    "VLProvider",
    "VideoPollConfig",
    "VideoProvider",
    "VideoResult",
    "WanMode",
    "WanSpec",
    "classify_status",
    "cosine",
    "create_providers",
    "data_uri",
    "sdk_get",
]
