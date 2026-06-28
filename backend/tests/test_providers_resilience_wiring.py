"""Tests for the additive gateway wiring: config translation + the opt-in seam
in ``create_providers``. The default path (gateway disabled) must be unchanged.
"""

from __future__ import annotations

from app.core.config import Settings
from app.providers import create_providers
from app.providers.resilience import (
    Capability,
    JitterStrategy,
    build_gateway,
    gateway_config_from_settings,
    gateway_serves,
    registry_from_settings,
)


def _settings(**over: object) -> Settings:
    return Settings(dashscope_api_key="test", **over)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Config translation
# --------------------------------------------------------------------------- #


def test_gateway_config_from_settings_maps_fields() -> None:
    s = _settings(
        provider_gateway_max_attempts=6,
        provider_gateway_backoff_base_s=0.25,
        provider_gateway_jitter="decorrelated",
        provider_gateway_breaker_failure_threshold=7,
        provider_gateway_cache_ttl_s=120.0,
        provider_gateway_hedge_max_attempts=2,
    )
    cfg = gateway_config_from_settings(s)
    assert cfg.max_attempts == 6
    assert cfg.backoff.base_s == 0.25
    assert cfg.backoff.strategy is JitterStrategy.DECORRELATED
    assert cfg.breaker.failure_threshold == 7
    assert cfg.cache.ttl_s == 120.0
    assert cfg.hedge.max_attempts == 2


def test_gateway_config_unknown_jitter_falls_back_to_full() -> None:
    s = _settings(provider_gateway_jitter="bogus")
    cfg = gateway_config_from_settings(s)
    assert cfg.backoff.strategy is JitterStrategy.FULL


def test_build_gateway_fans_metering_into_usage_sink() -> None:
    from app.providers.types import Usage

    seen: list[str] = []
    gw = build_gateway(_settings(), usage_sink=lambda u: seen.append(u.model))
    # The gateway's metering sink forwards to the provided usage sink.
    gw.metering(Usage(model="qwen3.7-max", operation="chat", input_tokens=1))
    assert seen == ["qwen3.7-max"]
    assert gw.metering.downstream_count == 1


# --------------------------------------------------------------------------- #
# Registry from settings
# --------------------------------------------------------------------------- #


def test_registry_from_settings_dashscope_only_by_default() -> None:
    reg = registry_from_settings(_settings())
    assert reg.get("dashscope") is not None
    assert reg.get("openai") is None
    # DashScope serves chat + every video mode.
    assert gateway_serves(reg, Capability.CHAT)
    assert gateway_serves(reg, Capability.VIDEO_R2V)
    chat = reg.negotiate(Capability.CHAT)
    assert chat.preferred is not None and chat.preferred.name == "dashscope"


def test_registry_from_settings_adds_openai_when_reasoning_openai() -> None:
    reg = registry_from_settings(
        _settings(reasoning_provider="openai", openai_api_key="sk-test", reasoning_model="gpt-5.5")
    )
    assert reg.get("openai") is not None
    chat = reg.negotiate(Capability.CHAT)
    # OpenAI (priority 5) leads chat; only DashScope serves video.
    assert chat.preferred is not None and chat.preferred.name == "openai"
    assert chat.chosen_model == "gpt-5.5"
    video = reg.negotiate(Capability.VIDEO_T2V)
    assert video.preferred is not None and video.preferred.name == "dashscope"


# --------------------------------------------------------------------------- #
# The opt-in create_providers seam
# --------------------------------------------------------------------------- #


def test_create_providers_gateway_disabled_by_default() -> None:
    providers = create_providers(_settings())
    assert providers.gateway is None
    assert providers.registry is None


def test_create_providers_builds_gateway_when_enabled() -> None:
    providers = create_providers(_settings(provider_gateway_enabled=True))
    assert providers.gateway is not None
    assert providers.registry is not None
    # The gateway exists and its snapshot is well-formed.
    snap = providers.gateway.snapshot()
    assert snap.calls.calls == 0
    assert providers.registry.get("dashscope") is not None


def test_create_providers_gateway_metering_shares_client_sink() -> None:
    # The gateway's metering must fan into the same usage sink the client uses, so
    # cost/budget accounting stays unified when the gateway is on.
    providers = create_providers(_settings(provider_gateway_enabled=True))
    assert providers.gateway is not None
    # The client's default sink is a downstream of the gateway's metering.
    assert providers.gateway.metering.downstream_count == 1
