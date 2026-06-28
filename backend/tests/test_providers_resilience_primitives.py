"""Unit tests for the resilience gateway primitives.

Covers the deterministic, no-I/O building blocks: backoff schedules, the adaptive
(AIMD) token bucket, the per-model breaker registry, and the request-hash response
cache + single-flight dedup. Time/RNG are injected throughout so nothing waits on
the wall clock.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from app.providers.errors import CircuitOpenError
from app.providers.resilience.backoff import BackoffPolicy, BackoffSchedule, JitterStrategy
from app.providers.resilience.breakers import (
    BreakerConfig,
    BreakerRegistry,
    BreakerState,
)
from app.providers.resilience.cache import CacheConfig, ResponseCache, request_hash
from app.providers.resilience.chaos import FakeClock, make_async_sleep
from app.providers.resilience.ratelimit import AdaptiveRateConfig, AdaptiveTokenBucket

# --------------------------------------------------------------------------- #
# Backoff schedules
# --------------------------------------------------------------------------- #


def test_backoff_none_is_pure_exponential_capped() -> None:
    sched = BackoffSchedule(BackoffPolicy(base_s=1.0, multiplier=2.0, max_s=10.0,
                                          strategy=JitterStrategy.NONE))
    assert sched.next_delay(1) == 1.0
    assert sched.next_delay(2) == 2.0
    assert sched.next_delay(3) == 4.0
    assert sched.next_delay(4) == 8.0
    assert sched.next_delay(5) == 10.0  # capped at max_s
    assert sched.next_delay(99) == 10.0


def test_backoff_full_jitter_stays_within_cap() -> None:
    rng = random.Random(7)
    sched = BackoffSchedule(
        BackoffPolicy(base_s=1.0, multiplier=2.0, max_s=8.0, strategy=JitterStrategy.FULL),
        rng=rng,
    )
    for attempt in range(1, 6):
        cap = min(1.0 * 2 ** (attempt - 1), 8.0)
        delay = sched.next_delay(attempt)
        assert 0.0 <= delay <= cap


def test_backoff_equal_jitter_keeps_a_floor() -> None:
    # Equal jitter: delay in [cap/2, cap]; attempt 2 cap = 4 -> [2, 4].
    policy = BackoffPolicy(base_s=2.0, multiplier=2.0, max_s=100.0, strategy=JitterStrategy.EQUAL)
    for seed in range(20):
        s = BackoffSchedule(policy, rng=random.Random(seed))
        delay = s.next_delay(2)
        assert 2.0 <= delay <= 4.0


def test_backoff_decorrelated_walks_from_base() -> None:
    sched = BackoffSchedule(
        BackoffPolicy(base_s=1.0, max_s=50.0, strategy=JitterStrategy.DECORRELATED),
        rng=random.Random(3),
    )
    # First decorrelated delay is uniform(base, base*3) = [1, 3].
    first = sched.next_delay(1)
    assert 1.0 <= first <= 3.0
    second = sched.next_delay(2)
    assert 1.0 <= second <= 50.0


def test_backoff_respects_retry_after_as_floor() -> None:
    sched = BackoffSchedule(
        BackoffPolicy(base_s=0.1, max_s=1.0, strategy=JitterStrategy.NONE, retry_after_cap_s=30.0)
    )
    # Computed exp delay is small; the server says wait 5s -> we wait at least 5.
    assert sched.next_delay(1, retry_after_s=5.0) == 5.0
    # A hostile huge Retry-After is capped.
    assert sched.next_delay(1, retry_after_s=999.0) == 30.0


def test_backoff_policy_validates() -> None:
    with pytest.raises(ValueError):
        BackoffPolicy(base_s=0.0)
    with pytest.raises(ValueError):
        BackoffPolicy(base_s=2.0, max_s=1.0)
    with pytest.raises(ValueError):
        BackoffPolicy(multiplier=0.5)


# --------------------------------------------------------------------------- #
# Adaptive token bucket (AIMD)
# --------------------------------------------------------------------------- #


async def test_adaptive_bucket_throttle_halves_rate_and_cools_down() -> None:
    clock = FakeClock()
    bucket = AdaptiveTokenBucket(
        AdaptiveRateConfig(initial_rate=8.0, decrease_factor=0.5, cooldown_s=5.0),
        clock=clock,
        sleep=make_async_sleep(clock),
    )
    assert bucket.rate == 8.0
    bucket.record_throttle()
    assert bucket.rate == 4.0
    assert bucket.in_cooldown() is True
    # Successes during cooldown do NOT increase the rate.
    bucket.record_success()
    assert bucket.rate == 4.0
    # After cooldown elapses, a success bumps the rate (additive increase).
    clock.advance(5.0)
    assert bucket.in_cooldown() is False
    bucket.record_success()
    assert bucket.rate == pytest.approx(4.25)


async def test_adaptive_bucket_rate_floor_and_ceiling() -> None:
    cfg = AdaptiveRateConfig(initial_rate=2.0, min_rate=0.5, max_rate=4.0,
                             increase_step=1.0, decrease_factor=0.5, cooldown_s=0.0)
    bucket = AdaptiveTokenBucket(cfg)
    for _ in range(10):
        bucket.record_throttle()
    assert bucket.rate == 0.5  # clamped at min_rate
    for _ in range(20):
        bucket.record_success()
    assert bucket.rate == 4.0  # clamped at max_rate
    assert bucket.throttle_events == 10
    assert bucket.success_events == 20


async def test_adaptive_bucket_acquire_waits_via_injected_sleep() -> None:
    clock = FakeClock()
    bucket = AdaptiveTokenBucket(
        AdaptiveRateConfig(initial_rate=10.0, burst=1),
        clock=clock,
        sleep=make_async_sleep(clock),
    )
    await bucket.acquire()  # consumes the single burst token at t=0
    # Next acquire must wait ~0.1s (1 token / 10 rate); the fake sleep advances clock.
    await bucket.acquire()
    assert clock.now >= 0.1


def test_adaptive_config_validates() -> None:
    with pytest.raises(ValueError):
        AdaptiveRateConfig(decrease_factor=1.5)
    with pytest.raises(ValueError):
        AdaptiveRateConfig(min_rate=0.0)
    with pytest.raises(ValueError):
        AdaptiveRateConfig(initial_rate=20.0, max_rate=10.0)


# --------------------------------------------------------------------------- #
# Per-model breaker registry
# --------------------------------------------------------------------------- #


async def test_breaker_trips_open_after_threshold_then_half_open_then_closes() -> None:
    clock = FakeClock()
    reg = BreakerRegistry(BreakerConfig(failure_threshold=3, recovery_s=10.0), clock=clock)
    b = await reg.get("qwen-image-plus")
    assert b.state is BreakerState.CLOSED
    for _ in range(3):
        await b.before_call()
        await b.record_failure()
    assert b.state is BreakerState.OPEN
    # Open breaker rejects without attempting.
    with pytest.raises(CircuitOpenError):
        await b.before_call()
    # After the cooldown, the next before_call enters HALF_OPEN and admits a probe.
    clock.advance(10.0)
    await b.before_call()
    assert b.state is BreakerState.HALF_OPEN
    await b.record_success()  # probe succeeds -> closed
    assert b.state is BreakerState.CLOSED


async def test_breaker_half_open_failure_reopens() -> None:
    clock = FakeClock()
    reg = BreakerRegistry(BreakerConfig(failure_threshold=2, recovery_s=5.0), clock=clock)
    b = await reg.get("wan2.1-i2v-turbo")
    for _ in range(2):
        await b.before_call()
        await b.record_failure()
    assert b.state is BreakerState.OPEN
    clock.advance(5.0)
    await b.before_call()  # half-open probe
    assert b.state is BreakerState.HALF_OPEN
    await b.record_failure()  # probe fails -> back to open
    assert b.state is BreakerState.OPEN


async def test_breaker_half_open_probe_budget_limits_concurrency() -> None:
    clock = FakeClock()
    reg = BreakerRegistry(
        BreakerConfig(failure_threshold=1, recovery_s=1.0, half_open_max_calls=1), clock=clock
    )
    b = await reg.get("m")
    await b.before_call()
    await b.record_failure()
    clock.advance(1.0)
    await b.before_call()  # reserves the single probe slot
    # A second concurrent probe is rejected (budget exhausted).
    with pytest.raises(CircuitOpenError):
        await b.before_call()


async def test_breaker_registry_is_per_model_independent() -> None:
    clock = FakeClock()
    reg = BreakerRegistry(BreakerConfig(failure_threshold=2, recovery_s=10.0), clock=clock)
    image = await reg.get("qwen-image-plus")
    chat = await reg.get("qwen3.7-max")
    for _ in range(2):
        await image.before_call()
        await image.record_failure()
    assert image.state is BreakerState.OPEN
    # Chat is untouched: one model's outage does not starve another.
    assert chat.state is BreakerState.CLOSED
    await chat.before_call()
    await chat.record_success()
    assert chat.state is BreakerState.CLOSED
    assert {s.model for s in reg.snapshots()} == {"qwen-image-plus", "qwen3.7-max"}


async def test_breaker_registry_lazy_creation_and_peek() -> None:
    reg = BreakerRegistry()
    assert reg.peek("x") is None
    b = await reg.get("x")
    assert reg.peek("x") is b
    assert await reg.get("x") is b  # idempotent


def test_breaker_config_validates() -> None:
    with pytest.raises(ValueError):
        BreakerConfig(failure_threshold=0)
    with pytest.raises(ValueError):
        BreakerConfig(half_open_max_calls=0)
    with pytest.raises(ValueError):
        BreakerConfig(half_open_max_calls=1, half_open_success_threshold=2)


# --------------------------------------------------------------------------- #
# Response cache + single-flight dedup
# --------------------------------------------------------------------------- #


def test_request_hash_is_order_independent() -> None:
    a = request_hash("m", "chat", {"b": 1, "a": 2})
    b = request_hash("m", "chat", {"a": 2, "b": 1})
    assert a == b
    # Different model / op / payload -> different key.
    assert request_hash("m2", "chat", {"a": 2}) != request_hash("m", "chat", {"a": 2})
    assert request_hash("m", "vl", {"a": 2}) != request_hash("m", "chat", {"a": 2})


def test_request_hash_handles_bytes_payload() -> None:
    # Bytes are summarized (length + short digest), never hashed inline wholesale.
    key1 = request_hash("img", "image", {"ref": b"\x00\x01\x02"})
    key2 = request_hash("img", "image", {"ref": b"\x00\x01\x02"})
    key3 = request_hash("img", "image", {"ref": b"\x00\x01\x03"})
    assert key1 == key2
    assert key1 != key3


async def test_cache_hit_miss_ttl_and_lru() -> None:
    clock = FakeClock()
    cache: ResponseCache[str] = ResponseCache(
        CacheConfig(max_entries=2, ttl_s=10.0), clock=clock
    )
    await cache.set("a", "1")
    assert await cache.get("a") == "1"
    assert cache.stats.hits == 1
    # TTL expiry.
    clock.advance(10.0)
    assert await cache.get("a") is None
    assert cache.stats.expirations == 1
    # LRU eviction at capacity 2.
    clock.advance(0.0)
    await cache.set("x", "1")
    await cache.set("y", "2")
    await cache.set("z", "3")  # evicts the LRU ("x")
    assert await cache.get("x") is None
    assert await cache.get("y") == "2"
    assert cache.stats.evictions == 1


async def test_cache_single_flight_coalesces_concurrent_misses() -> None:
    cache: ResponseCache[int] = ResponseCache(CacheConfig(single_flight=True))
    calls = 0
    gate = asyncio.Event()

    async def compute() -> int:
        nonlocal calls
        calls += 1
        await gate.wait()
        return 42

    # Launch three concurrent get_or_compute for the same key.
    tasks = [asyncio.create_task(cache.get_or_compute("k", compute)) for _ in range(3)]
    await asyncio.sleep(0)  # let them register on the leader
    gate.set()
    results = await asyncio.gather(*tasks)
    assert results == [42, 42, 42]
    assert calls == 1  # only the leader actually computed
    assert cache.stats.coalesced == 2


async def test_cache_does_not_cache_compute_errors() -> None:
    cache: ResponseCache[int] = ResponseCache(CacheConfig())
    attempts = 0

    async def boom() -> int:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await cache.get_or_compute("k", boom)
    # A second call re-runs compute (the error was not cached).
    with pytest.raises(RuntimeError):
        await cache.get_or_compute("k", boom)
    assert attempts == 2


async def test_cache_invalidate_and_clear() -> None:
    cache: ResponseCache[str] = ResponseCache(CacheConfig())
    await cache.set("a", "1")
    assert await cache.invalidate("a") is True
    assert await cache.invalidate("a") is False
    await cache.set("b", "2")
    await cache.clear()
    assert len(cache) == 0
