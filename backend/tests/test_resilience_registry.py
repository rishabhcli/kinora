"""Tests for the per-dependency registries (lazy creation + isolation)."""

from __future__ import annotations

from app.resilience.breaker import BreakerConfig, CircuitBreaker
from app.resilience.bulkhead import Bulkhead, BulkheadConfig
from app.resilience.clock import ManualClock
from app.resilience.ratelimit import SlidingWindowConfig, SlidingWindowLimiter
from app.resilience.registry import (
    BreakerRegistry,
    BulkheadRegistry,
    RateLimiterRegistry,
    ResilienceRegistry,
)


async def test_breaker_registry_lazy_and_memoized() -> None:
    reg = BreakerRegistry(clock=ManualClock())
    a1 = await reg.get("dashscope.image")
    a2 = await reg.get("dashscope.image")
    b = await reg.get("redis.queue")
    assert a1 is a2  # memoized per name
    assert a1 is not b  # isolated per dependency
    assert set(reg.names()) == {"dashscope.image", "redis.queue"}


async def test_breaker_registry_per_name_override() -> None:
    reg = BreakerRegistry(
        BreakerConfig(consecutive_failure_threshold=10),
        overrides={"flaky": BreakerConfig(consecutive_failure_threshold=1)},
    )
    flaky = await reg.get("flaky")
    normal = await reg.get("normal")
    assert flaky.config.consecutive_failure_threshold == 1
    assert normal.config.consecutive_failure_threshold == 10


async def test_breaker_isolation_one_open_does_not_affect_other() -> None:
    reg = BreakerRegistry(BreakerConfig(consecutive_failure_threshold=1))
    img = await reg.get("image")
    chat = await reg.get("chat")
    await img.before_call()
    await img.record_failure()
    from app.resilience.breaker import BreakerState

    assert img.state is BreakerState.OPEN
    assert chat.state is BreakerState.CLOSED
    await chat.before_call()  # chat still admits


async def test_register_prebuilt_instances() -> None:
    breg = BreakerRegistry()
    custom = CircuitBreaker("custom", BreakerConfig(cooldown_s=99.0))
    breg.register("custom", custom)
    assert await breg.get("custom") is custom

    hreg = BulkheadRegistry()
    bh = Bulkhead("hh", BulkheadConfig(max_concurrency=3))
    hreg.register("hh", bh)
    assert await hreg.get("hh") is bh


async def test_bulkhead_registry_lazy() -> None:
    reg = BulkheadRegistry(BulkheadConfig(max_concurrency=2))
    a = await reg.get("dep")
    assert (await reg.get("dep")) is a
    assert a.config.max_concurrency == 2


async def test_ratelimiter_registry_default_and_override() -> None:
    sw = SlidingWindowLimiter("strict", SlidingWindowConfig(max_events=5, window_s=1.0))
    reg = RateLimiterRegistry(overrides={"strict": sw})
    assert await reg.get("strict") is sw
    default = await reg.get("loose")  # falls back to a default token bucket
    assert default.name == "loose"


async def test_resilience_registry_bundles_all_three() -> None:
    clock = ManualClock()
    reg = ResilienceRegistry(clock=clock)
    br = await reg.breakers.get("d")
    bh = await reg.bulkheads.get("d")
    rl = await reg.rate_limiters.get("d")
    assert br.name == "d"
    assert bh.name == "d"
    assert rl.name == "d"
    health = reg.health()
    assert "breakers" in health and "bulkheads" in health and "rate_limiters" in health
