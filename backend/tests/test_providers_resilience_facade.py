"""Tests for the gateway-wrapped provider facades (composition with round-1).

Uses a real ``ChatProvider`` over a ``ChaosTransport``-backed ``ProviderClient``,
wrapped by ``GatewayChatProvider``, so the full chain (gateway -> ChatProvider ->
HTTP parse) is exercised against scripted faults. Also covers the generic
``GatewayCallable`` and the video-never-hedged invariant.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.providers.base import ProviderClient, ResilienceConfig
from app.providers.chat import ChatProvider
from app.providers.errors import ProviderBadRequest
from app.providers.resilience.chaos import (
    ChaosTransport,
    FakeClock,
    FaultKind,
    FaultPlan,
    FaultProfile,
    make_async_sleep,
)
from app.providers.resilience.facade import GatewayCallable, GatewayChatProvider
from app.providers.resilience.gateway import (
    GatewayCall,
    GatewayConfig,
    ResilientGateway,
)


def _settings() -> Settings:
    return Settings(dashscope_api_key="test")


def _client(chaos: ChaosTransport) -> ProviderClient:
    # Single-attempt round-1 client so the gateway's outer loop owns the retries.
    return ProviderClient(
        _settings(),
        transport=chaos.transport(),
        resilience=ResilienceConfig(max_attempts=1, rate_per_s=1000, rate_burst=1000),
    )


def _gateway(clock: FakeClock, **over: object) -> ResilientGateway:
    cfg: dict[str, object] = {"max_attempts": 4}
    cfg.update(over)
    return ResilientGateway(
        GatewayConfig(**cfg),  # type: ignore[arg-type]
        clock=clock,
        sleep=make_async_sleep(clock),
    )


# --------------------------------------------------------------------------- #
# GatewayChatProvider over the real ChatProvider + ChaosTransport
# --------------------------------------------------------------------------- #


async def test_gateway_chat_recovers_through_real_chatprovider() -> None:
    clock = FakeClock()
    # Fail once (5xx) then return a valid chat body.
    chaos = ChaosTransport(
        FaultProfile(FaultPlan(sequence=(FaultKind.SERVER_ERROR,))), model="qwen3.7-max"
    )
    client = _client(chaos)
    facade = GatewayChatProvider(ChatProvider(client), _gateway(clock))
    result = await facade.chat([{"role": "user", "content": "hi"}], "qwen3.7-max", hedge=False)
    assert result.model == "qwen3.7-max"
    # One failure + one success = two chaos requests.
    assert len(chaos.requests) == 2
    await client.aclose()


async def test_gateway_chat_json_caches_identical_prompt() -> None:
    clock = FakeClock()
    chaos = ChaosTransport(
        FaultProfile(FaultPlan(sequence=(), terminal=FaultKind.OK)), model="qwen3.7-max"
    )
    client = _client(chaos)
    gw = _gateway(clock)
    facade = GatewayChatProvider(ChatProvider(client), gw)
    messages = [{"role": "user", "content": "extract"}]
    # Force the non-streaming path (chat_json streams by default); the chaos
    # transport serves a non-streaming chat-completion body. ``stream`` is dropped
    # from the cache key (a transport knob, not content), so identity still holds.
    a = await facade.chat_json(messages, "qwen3.7-max", stream=False)
    b = await facade.chat_json(messages, "qwen3.7-max", stream=False)
    assert a == b
    # The second identical structured prompt was served from cache -> one HTTP call.
    assert len(chaos.requests) == 1
    cache_stats = gw.snapshot().cache
    assert cache_stats is not None and cache_stats.hits == 1
    await client.aclose()


async def test_gateway_chat_bad_request_is_terminal() -> None:
    clock = FakeClock()
    chaos = ChaosTransport(
        FaultProfile(FaultPlan(sequence=(), terminal=FaultKind.BAD_REQUEST)), model="qwen3.7-max"
    )
    client = _client(chaos)
    facade = GatewayChatProvider(ChatProvider(client), _gateway(clock))
    with pytest.raises(ProviderBadRequest):
        await facade.chat([{"role": "user", "content": "x"}], "qwen3.7-max", hedge=False)
    assert len(chaos.requests) == 1  # 4xx short-circuits
    await client.aclose()


# --------------------------------------------------------------------------- #
# GatewayCallable (generic wrapper)
# --------------------------------------------------------------------------- #


async def test_gateway_callable_routes_arbitrary_method() -> None:
    clock = FakeClock()
    gw = _gateway(clock)
    runner = GatewayCallable(gw)
    calls = 0

    async def embed(text: str) -> list[float]:
        nonlocal calls
        calls += 1
        return [float(len(text))]

    policy = GatewayCall(
        model="tongyi-embedding-vision-plus",
        op="embed",
        cacheable=True,
        cache_payload={"text": "hello"},
    )
    a = await runner.run(policy, embed, "hello")
    b = await runner.run(policy, embed, "hello")
    assert a == b == [5.0]
    assert calls == 1  # the second was a cache hit


async def test_gateway_callable_video_never_hedged() -> None:
    clock = FakeClock()
    gw = ResilientGateway(
        GatewayConfig(max_attempts=1),  # hedge defaults to max_attempts=1 -> off
        clock=clock,
        sleep=make_async_sleep(clock),
    )
    runner = GatewayCallable(gw)
    renders = 0

    async def render() -> str:
        nonlocal renders
        renders += 1
        return "clip"

    policy = GatewayCall(model="wan2.1-i2v-turbo", op="video", idempotent=False)
    out = await runner.run(policy, render)
    assert out == "clip"
    assert renders == 1
