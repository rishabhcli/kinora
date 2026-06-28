"""Build the resilience gateway + multi-cloud registry from :class:`Settings`.

The single place that translates the (additive) ``provider_gateway_*`` settings
into a :class:`~app.providers.resilience.gateway.GatewayConfig` and a populated
:class:`~app.providers.resilience.registry.ProviderRegistry`. Keeping the mapping
here means the gateway/registry stay pure (no settings imports) and the wiring is
one tested function.

Nothing here is called unless ``settings.provider_gateway_enabled`` is True; the
default provider path is untouched (see ``providers.__init__.create_providers``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.logging import get_logger

from .backoff import BackoffPolicy, JitterStrategy
from .breakers import BreakerConfig
from .cache import CacheConfig
from .gateway import GatewayConfig, ResilientGateway
from .hedging import HedgePolicy
from .metering import MeteringSink, UsageSink
from .ratelimit import AdaptiveRateConfig, AdaptiveTokenBucket
from .registry import (
    Capability,
    ProviderRegistry,
    dashscope_descriptor,
    openai_descriptor,
)

if TYPE_CHECKING:
    from app.core.config import Settings

logger = get_logger("app.providers.resilience.factory")


def _jitter(value: str) -> JitterStrategy:
    try:
        return JitterStrategy(value.lower())
    except ValueError:
        logger.warning("gateway.unknown_jitter", value=value)
        return JitterStrategy.FULL


def gateway_config_from_settings(settings: Settings) -> GatewayConfig:
    """Translate the ``provider_gateway_*`` settings into a :class:`GatewayConfig`."""
    return GatewayConfig(
        max_attempts=settings.provider_gateway_max_attempts,
        backoff=BackoffPolicy(
            base_s=settings.provider_gateway_backoff_base_s,
            max_s=settings.provider_gateway_backoff_max_s,
            strategy=_jitter(settings.provider_gateway_jitter),
        ),
        breaker=BreakerConfig(
            failure_threshold=settings.provider_gateway_breaker_failure_threshold,
            recovery_s=settings.provider_gateway_breaker_recovery_s,
            half_open_max_calls=settings.provider_gateway_breaker_half_open_max_calls,
        ),
        cache=CacheConfig(
            max_entries=settings.provider_gateway_cache_max_entries,
            ttl_s=settings.provider_gateway_cache_ttl_s,
        ),
        hedge=HedgePolicy(
            max_attempts=settings.provider_gateway_hedge_max_attempts,
            delay_s=settings.provider_gateway_hedge_delay_s,
        ),
        cache_enabled=settings.provider_gateway_cache_enabled,
    )


def adaptive_bucket_from_settings(settings: Settings) -> AdaptiveTokenBucket:
    """Build the gateway's adaptive token bucket from settings."""
    return AdaptiveTokenBucket(
        AdaptiveRateConfig(
            initial_rate=settings.provider_gateway_rate_initial,
            max_rate=settings.provider_gateway_rate_max,
            min_rate=settings.provider_gateway_rate_min,
            burst=settings.provider_gateway_rate_burst,
            cooldown_s=settings.provider_gateway_rate_cooldown_s,
        )
    )


def build_gateway(
    settings: Settings, *, usage_sink: UsageSink | None = None
) -> ResilientGateway:
    """Construct a :class:`ResilientGateway` wired to settings + an optional sink.

    The gateway's metering sink fans out to ``usage_sink`` (e.g. the same sink the
    round-1 client uses, keeping cost/budget unified), so adding the gateway never
    splits the spend ledger.
    """
    metering = MeteringSink([usage_sink] if usage_sink is not None else None)
    return ResilientGateway(
        gateway_config_from_settings(settings),
        metering=metering,
        rate_limiter=adaptive_bucket_from_settings(settings),
    )


def registry_from_settings(settings: Settings) -> ProviderRegistry:
    """Build the multi-cloud capability registry from the configured model ids.

    DashScope is always registered (it serves nearly everything). OpenAI is
    registered as a chat-only lane *only* when ``reasoning_provider == "openai"``
    and a key is present — mirroring ``create_providers``' chat-routing decision so
    the registry's capability view matches the live wiring.
    """
    registry = ProviderRegistry(
        [
            dashscope_descriptor(
                chat_models=[
                    settings.chat_model_max,
                    settings.chat_model_plus,
                    settings.chat_model_adapter,
                ],
                vl_models=[settings.vl_model],
                image_models=[settings.image_model],
                image_edit_models=[settings.image_edit_model],
                tts_models=[settings.tts_model, settings.tts_clone_model],
                embed_models=[settings.embed_model_image, settings.embed_model_text],
                t2v_models=[settings.video_model],
                i2v_models=[settings.video_model_i2v],
                r2v_models=[settings.video_model_r2v],
            )
        ]
    )
    if settings.reasoning_provider.lower() == "openai" and settings.openai_api_key:
        registry.register(openai_descriptor(chat_models=[settings.reasoning_model]))
    return registry


def gateway_serves(registry: ProviderRegistry, capability: Capability) -> bool:
    """Convenience: does any registered provider serve ``capability``?"""
    return bool(registry.providers_for(capability))


__all__ = [
    "adaptive_bucket_from_settings",
    "build_gateway",
    "gateway_config_from_settings",
    "gateway_serves",
    "registry_from_settings",
]
