"""Real DashScope (Qwen Cloud) provider layer for Kinora.

Production integrations — chat, vision-language, image (gen + edit), TTS
(voice clone + narration), and hosted Wan video — over a shared resilient client
(:class:`ProviderClient`) with retries, a circuit breaker, a rate limiter, and a
cost-accounting sink. No mocks: every provider calls the live API. Real Wan video
renders are gated behind ``settings.kinora_live_video``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.config import Settings, get_settings

if TYPE_CHECKING:
    from .resilience import ProviderRegistry, ResilientGateway

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
from .minimax import MiniMaxBudgetExceeded, MiniMaxVideoProvider, RedisSpendStore
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
from .video_router import (
    BackendHealth,
    BackendStatus,
    BackendTier,
    RouteMode,
    RouterPolicy,
    VideoBackend,
    VideoRouter,
    order_for_budget,
)
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
    video: VideoProvider | MiniMaxVideoProvider
    embeddings: EmbeddingProvider
    # Set only when ``reasoning_provider="openai"``: the second transport that
    # backs ``chat`` (OpenAI), sharing the main client's usage sink.
    reasoning_client: ProviderClient | None = None
    # Set only when ``provider_gateway_enabled`` (round-2, opt-in): the hardened
    # resilience gateway + multi-cloud capability registry composed *around* the
    # shared client (per-model breakers, adaptive rate-limit, hedging, response
    # cache). ``None`` by default so the round-1 path is unchanged. See
    # ``app.providers.resilience``.
    gateway: ResilientGateway | None = None
    registry: ProviderRegistry | None = None

    async def aclose(self) -> None:
        await self.client.aclose()
        if self.reasoning_client is not None:
            await self.reasoning_client.aclose()
        if self.gateway is not None:
            await self.gateway.aclose()


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
    video: VideoProvider | MiniMaxVideoProvider
    if resolved.video_backend.lower() == "minimax" and resolved.minimax_api_key:
        # Share the main client's usage sink so MiniMax video-seconds land in the
        # same budget accounting as everything else.
        video = build_minimax_video_provider(resolved, usage_sink=client.usage_sink)
    else:
        video = VideoProvider(client)
    gateway: ResilientGateway | None = None
    registry: ProviderRegistry | None = None
    if resolved.provider_gateway_enabled:
        # Opt-in: build the resilience gateway fanning its metering into the same
        # usage sink the client uses, so cost/budget accounting stays unified.
        from .resilience import build_gateway, registry_from_settings

        gateway = build_gateway(resolved, usage_sink=client.usage_sink)
        registry = registry_from_settings(resolved)
    return Providers(
        client=client,
        reasoning_client=reasoning_client,
        chat=chat,
        vl=VLProvider(client),
        image=ImageProvider(client),
        tts=TtsProvider(client),
        video=video,
        embeddings=EmbeddingProvider(client),
        gateway=gateway,
        registry=registry,
    )


def create_video_router(
    client: ProviderClient,
    *,
    model_ids: Sequence[str],
    policy: RouterPolicy | None = None,
) -> VideoRouter:
    """Build a :class:`VideoRouter` over several hosted Wan model ids on one client.

    Each id becomes a :class:`VideoProvider` whose ``WanSpec.model`` defaults to
    that id (the router passes the spec through unchanged, so each backend resolves
    its own configured id only when the spec leaves ``model`` unset — callers that
    want a per-backend pin set ``spec.model`` accordingly). The first id is the
    preferred backend; the rest are failover/race candidates. All share the one
    resilient transport, so the budget/cost sink stays unified.

    This is the additive seam Phase 9 wires into ``create_providers`` behind a
    setting; today it is opt-in so the default single-backend path is unchanged.
    """
    if not model_ids:
        raise ValueError("create_video_router requires at least one model id")
    backends = [VideoProvider(client, name=f"video:{model_id}") for model_id in model_ids]
    return VideoRouter(backends, policy=policy)


def build_minimax_video_provider(
    settings: Settings,
    *,
    usage_sink: UsageSink | None = None,
) -> MiniMaxVideoProvider:
    """Build a MiniMax video backend on its own MiniMax-configured client.

    Uses ``base_url_override`` + ``api_key_override`` so the provider reuses the
    shared :class:`ProviderClient` resilience (retries/breaker/rate-limit) and the
    one usage sink (unified cost/budget), exactly like the OpenAI reasoning path.
    A Redis-backed :class:`RedisSpendStore` persists the cumulative-USD guard
    across restarts and across the api/render-worker processes.
    """
    mm_client = ProviderClient(
        settings,
        usage_sink=usage_sink,
        base_url_override=settings.minimax_base_url,
        api_key_override=settings.minimax_api_key,
    )
    from redis.asyncio import Redis

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    spend_store = RedisSpendStore(redis)
    return MiniMaxVideoProvider(mm_client, spend_store=spend_store)


__all__ = [
    "EMBED_DIM",
    "AuthenticationError",
    "BackendHealth",
    "BackendStatus",
    "BackendTier",
    "BreakerState",
    "ChatProvider",
    "ChatResult",
    "CircuitBreaker",
    "CircuitOpenError",
    "EmbeddingProvider",
    "ImageProvider",
    "LiveVideoDisabled",
    "LoggingUsageSink",
    "MiniMaxBudgetExceeded",
    "MiniMaxVideoProvider",
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
    "RouteMode",
    "RouterPolicy",
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
    "VideoBackend",
    "VideoPollConfig",
    "VideoProvider",
    "VideoResult",
    "VideoRouter",
    "WanMode",
    "WanSpec",
    "RedisSpendStore",
    "build_minimax_video_provider",
    "classify_status",
    "cosine",
    "create_providers",
    "create_video_router",
    "data_uri",
    "order_for_budget",
    "sdk_get",
]
