"""Real DashScope (Qwen Cloud) provider layer for Kinora.

Production integrations — chat, vision-language, image (gen + edit), TTS
(voice clone + narration), and hosted Wan video — over a shared resilient client
(:class:`ProviderClient`) with retries, a circuit breaker, a rate limiter, and a
cost-accounting sink. No mocks: every provider calls the live API. Real Wan video
renders are gated behind ``settings.kinora_live_video``.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings, get_settings

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
from .chat import ChatProvider, OpenAIChatProvider
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
    video: VideoProvider
    embeddings: EmbeddingProvider
    # Set only when ``reasoning_provider="openai"``: the second transport that
    # backs ``chat`` (OpenAI), sharing the main client's usage sink.
    reasoning_client: ProviderClient | None = None

    async def aclose(self) -> None:
        await self.client.aclose()
        if self.reasoning_client is not None:
            await self.reasoning_client.aclose()


def create_providers(
    settings: Settings | None = None,
    *,
    usage_sink: UsageSink | None = None,
    resilience: ResilienceConfig | None = None,
) -> Providers:
    """Construct a shared client and all providers bound to it.

    Image / TTS / Wan video / embeddings always ride the DashScope client. The
    chat/reasoning provider follows ``settings.reasoning_provider``: by default
    it shares the DashScope client; when set to ``openai`` it gets a second
    transport pointed at OpenAI (same usage sink, so budget/cost stays unified)
    and forces ``reasoning_model`` (GPT-5 line) for every reasoning agent.
    """
    resolved = settings or get_settings()
    client = ProviderClient(
        resolved,
        usage_sink=usage_sink,
        resilience=resilience,
    )
    reasoning_client: ProviderClient | None = None
    chat: ChatProvider
    if resolved.reasoning_provider.lower() == "openai" and resolved.openai_api_key:
        reasoning_client = ProviderClient(
            resolved,
            usage_sink=client.usage_sink,  # one sink -> unified cost/budget accounting
            resilience=resilience,
            base_url_override=resolved.openai_base_url,
            api_key_override=resolved.openai_api_key,
        )
        chat = OpenAIChatProvider(
            reasoning_client,
            model=resolved.reasoning_model,
            reasoning_effort=resolved.reasoning_effort,
            max_output_tokens=resolved.reasoning_max_output_tokens,
        )
    else:
        chat = ChatProvider(client)
    video = VideoProvider(client)
    return Providers(
        client=client,
        reasoning_client=reasoning_client,
        chat=chat,
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
    "OpenAIChatProvider",
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
