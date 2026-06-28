"""Integration tests for :class:`ResilientGateway` — the composed stack.

Drives the gateway with the deterministic chaos toolkit (scripted fault sequences,
a fake clock, an instant async sleep) so retries with multi-second backoff complete
instantly. Verifies the sacred invariants: the spend gate is never a fault,
non-retryable errors short-circuit, 429s shape the adaptive rate, the breaker
trips per-model, the cache dedups, and hedging only fires for idempotent ops.
"""

from __future__ import annotations

import random

import pytest

from app.providers.errors import (
    CircuitOpenError,
    LiveVideoDisabled,
    ProviderBadRequest,
    ProviderError,
    TransientProviderError,
)
from app.providers.resilience.backoff import BackoffPolicy, JitterStrategy
from app.providers.resilience.breakers import BreakerConfig, BreakerState
from app.providers.resilience.chaos import (
    ChaoticAttempt,
    FakeClock,
    FaultKind,
    FaultPlan,
    FaultProfile,
    make_async_sleep,
)
from app.providers.resilience.gateway import GatewayCall, GatewayConfig, ResilientGateway
from app.providers.resilience.hedging import HedgePolicy


def _gateway(clock: FakeClock, **cfg_over: object) -> ResilientGateway:
    defaults: dict[str, object] = {
        "max_attempts": 4,
        "backoff": BackoffPolicy(base_s=0.5, max_s=8.0, strategy=JitterStrategy.NONE),
        "breaker": BreakerConfig(failure_threshold=3, recovery_s=10.0),
    }
    defaults.update(cfg_over)
    config = GatewayConfig(**defaults)  # type: ignore[arg-type]
    return ResilientGateway(
        config, clock=clock, rng=random.Random(0), sleep=make_async_sleep(clock)
    )


def _chat_call(**kw: object) -> GatewayCall:
    return GatewayCall(model="qwen3.7-max", op="chat", **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Happy path + retry
# --------------------------------------------------------------------------- #


async def test_success_first_attempt() -> None:
    clock = FakeClock()
    gw = _gateway(clock)
    attempt = ChaoticAttempt(FaultProfile(FaultPlan(sequence=())), result="hi")
    out = await gw.execute(_chat_call(), attempt)
    assert out == "hi"
    snap = gw.snapshot()
    assert snap.calls.successes == 1
    assert snap.calls.retries == 0


async def test_retries_transient_then_succeeds() -> None:
    clock = FakeClock()
    gw = _gateway(clock)
    # Fail twice (timeout, 5xx) then succeed.
    plan = FaultPlan(sequence=(FaultKind.TIMEOUT, FaultKind.SERVER_ERROR))
    attempt = ChaoticAttempt(FaultProfile(plan), result="recovered")
    out = await gw.execute(_chat_call(), attempt)
    assert out == "recovered"
    assert attempt.invocations == 3
    snap = gw.snapshot()
    assert snap.calls.retries == 2
    assert snap.calls.successes == 1
    # The fake clock advanced by the (deterministic NONE-jitter) backoff sum.
    assert clock.now > 0.0


async def test_exhausts_retries_and_raises() -> None:
    clock = FakeClock()
    gw = _gateway(clock, max_attempts=3)
    plan = FaultPlan(sequence=(), terminal=FaultKind.SERVER_ERROR)  # always 5xx
    attempt = ChaoticAttempt(FaultProfile(plan))
    with pytest.raises(TransientProviderError):
        await gw.execute(_chat_call(), attempt)
    assert attempt.invocations == 3  # max_attempts
    assert gw.snapshot().calls.failures == 1


async def test_non_retryable_bad_request_short_circuits() -> None:
    clock = FakeClock()
    gw = _gateway(clock)
    plan = FaultPlan(sequence=(), terminal=FaultKind.BAD_REQUEST)
    attempt = ChaoticAttempt(FaultProfile(plan))
    with pytest.raises(ProviderBadRequest):
        await gw.execute(_chat_call(), attempt)
    # A 4xx fails identically on retry, so only one attempt is made.
    assert attempt.invocations == 1


# --------------------------------------------------------------------------- #
# The sacred spend gate
# --------------------------------------------------------------------------- #


async def test_live_video_disabled_propagates_and_is_never_a_fault() -> None:
    clock = FakeClock()
    gw = _gateway(clock)

    async def gated() -> str:
        raise LiveVideoDisabled("KINORA_LIVE_VIDEO is off")

    call = GatewayCall(model="wan2.1-i2v-turbo", op="video")
    with pytest.raises(LiveVideoDisabled):
        await gw.execute(call, gated)
    # The breaker for the video model must NOT have recorded a failure.
    breaker = gw.breakers.peek("wan2.1-i2v-turbo")
    assert breaker is not None
    assert breaker.state is BreakerState.CLOSED
    assert breaker.snapshot().total_failures == 0
    # And it was not counted as a gateway failure or retried.
    assert gw.snapshot().calls.failures == 0


# --------------------------------------------------------------------------- #
# 429 shapes the adaptive rate
# --------------------------------------------------------------------------- #


async def test_rate_limited_decreases_adaptive_rate_then_recovers() -> None:
    clock = FakeClock()
    gw = _gateway(clock)
    start_rate = gw.rate
    # One 429 (with a Retry-After) then success.
    plan = FaultPlan(sequence=(FaultKind.RATE_LIMIT,), retry_after_s=2.0)
    attempt = ChaoticAttempt(FaultProfile(plan), result="ok")
    out = await gw.execute(_chat_call(), attempt)
    assert out == "ok"
    snap = gw.snapshot()
    assert snap.calls.throttles_observed == 1
    # The adaptive bucket halved on the throttle (AIMD), so the rate dropped.
    assert gw.rate < start_rate
    # The Retry-After (2s) was respected as the backoff floor -> clock advanced >= 2.
    assert clock.now >= 2.0


# --------------------------------------------------------------------------- #
# Per-model breaker trips through the gateway
# --------------------------------------------------------------------------- #


async def test_breaker_opens_after_repeated_failures_then_rejects() -> None:
    clock = FakeClock()
    # max_attempts=1 so each execute() is exactly one provider attempt.
    gw = _gateway(
        clock, max_attempts=1, breaker=BreakerConfig(failure_threshold=3, recovery_s=10.0)
    )
    plan = FaultPlan(sequence=(), terminal=FaultKind.SERVER_ERROR)
    for _ in range(3):
        attempt = ChaoticAttempt(FaultProfile(plan))
        with pytest.raises(TransientProviderError):
            await gw.execute(_chat_call(), attempt)
    breaker = gw.breakers.peek("qwen3.7-max")
    assert breaker is not None and breaker.state is BreakerState.OPEN
    # The next call is rejected by the open breaker without attempting.
    rejected = ChaoticAttempt(FaultProfile(FaultPlan(sequence=())))
    with pytest.raises(CircuitOpenError):
        await gw.execute(_chat_call(), rejected)
    assert rejected.invocations == 0
    assert gw.snapshot().calls.breaker_rejections == 1


async def test_breaker_recovers_via_half_open_probe() -> None:
    clock = FakeClock()
    gw = _gateway(
        clock, max_attempts=1, breaker=BreakerConfig(failure_threshold=2, recovery_s=5.0)
    )
    fail_plan = FaultPlan(sequence=(), terminal=FaultKind.SERVER_ERROR)
    for _ in range(2):
        with pytest.raises(ProviderError):
            await gw.execute(_chat_call(), ChaoticAttempt(FaultProfile(fail_plan)))
    breaker = gw.breakers.peek("qwen3.7-max")
    assert breaker is not None and breaker.state is BreakerState.OPEN
    # Advance past the cooldown; the next call probes (half-open) and succeeds.
    clock.advance(5.0)
    probe = ChaoticAttempt(FaultProfile(FaultPlan(sequence=())), result="up")
    out = await gw.execute(_chat_call(), probe)
    assert out == "up"
    assert breaker.state is BreakerState.CLOSED


# --------------------------------------------------------------------------- #
# Response cache + dedup through the gateway
# --------------------------------------------------------------------------- #


async def test_cacheable_call_serves_second_request_from_cache() -> None:
    clock = FakeClock()
    gw = _gateway(clock)
    invocations = 0

    async def attempt() -> str:
        nonlocal invocations
        invocations += 1
        return "computed"

    payload = {"messages": [{"role": "user", "content": "hi"}]}
    call = _chat_call(cacheable=True, cache_payload=payload)
    a = await gw.execute(call, attempt)
    b = await gw.execute(call, attempt)
    assert a == b == "computed"
    assert invocations == 1  # the second call hit the cache
    assert gw.snapshot().calls.cache_hits == 1


async def test_uncacheable_call_always_recomputes() -> None:
    clock = FakeClock()
    gw = _gateway(clock)
    invocations = 0

    async def attempt() -> str:
        nonlocal invocations
        invocations += 1
        return "x"

    call = _chat_call(cacheable=False)
    await gw.execute(call, attempt)
    await gw.execute(call, attempt)
    assert invocations == 2


# --------------------------------------------------------------------------- #
# Hedging through the gateway (idempotent only)
# --------------------------------------------------------------------------- #


async def test_idempotent_call_hedges_for_tail_latency() -> None:
    clock = FakeClock()
    gw = ResilientGateway(
        GatewayConfig(
            max_attempts=1,
            backoff=BackoffPolicy(strategy=JitterStrategy.NONE),
            hedge=HedgePolicy(max_attempts=2, delay_s=0.0),
        ),
        clock=clock,
        sleep=make_async_sleep(clock),
    )
    import asyncio

    release = asyncio.Event()

    async def attempt() -> str:
        # Both copies share this thunk; the gateway's hedger launches it twice.
        # Make the first call block and later ones return fast by toggling state.
        if not release.is_set():
            release.set()
            await asyncio.sleep(100)  # first copy hangs (will be cancelled)
            return "slow"
        return "hedge"

    call = _chat_call(idempotent=True)
    out = await gw.execute(call, attempt)
    assert out == "hedge"


async def test_video_render_is_never_hedged_even_if_marked_idempotent_off() -> None:
    # A video call is constructed with idempotent=False (the gateway policy), so the
    # hedger is never engaged regardless of hedge config — proving no double-spend.
    clock = FakeClock()
    gw = ResilientGateway(
        GatewayConfig(max_attempts=1, hedge=HedgePolicy(max_attempts=3, delay_s=0.0)),
        clock=clock,
        sleep=make_async_sleep(clock),
    )
    invocations = 0

    async def render() -> str:
        nonlocal invocations
        invocations += 1
        return "clip"

    call = GatewayCall(model="wan2.1-i2v-turbo", op="video", idempotent=False)
    out = await gw.execute(call, render)
    assert out == "clip"
    assert invocations == 1  # exactly one render, never duplicated
    assert gw._hedger.stats.calls == 0  # hedger not engaged at all


# --------------------------------------------------------------------------- #
# Snapshot shape
# --------------------------------------------------------------------------- #


async def test_snapshot_is_json_serializable() -> None:
    import json

    clock = FakeClock()
    gw = _gateway(clock)
    await gw.execute(_chat_call(), ChaoticAttempt(FaultProfile(FaultPlan(sequence=()))))
    payload = gw.snapshot().as_dict()
    # Round-trips through JSON (the debug route would emit this).
    assert json.loads(json.dumps(payload))["calls"]["successes"] == 1
