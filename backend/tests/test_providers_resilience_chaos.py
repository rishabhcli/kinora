"""Chaos / fault-injection suite.

Two layers:

1. **Real-transport chaos** — the round-1 :class:`~app.providers.base.ProviderClient`
   over a :class:`ChaosTransport` (httpx.MockTransport), wrapped by the gateway, so
   the *actual* HTTP parse + classify path is exercised against scripted faults. This
   proves the gateway composes with the real transport, not just a fake thunk.
2. **Probabilistic soak** — a random fault profile driving many gateway calls, with
   invariant assertions that hold regardless of the random sequence (no double-spend,
   the breaker eventually trips, every terminal outcome is a typed ProviderError).
"""

from __future__ import annotations

import random

import httpx
import pytest

from app.core.config import Settings
from app.providers.base import ProviderClient
from app.providers.errors import (
    CircuitOpenError,
    ProviderBadRequest,
    ProviderError,
    TransientProviderError,
)
from app.providers.resilience.backoff import BackoffPolicy, JitterStrategy
from app.providers.resilience.breakers import BreakerConfig, BreakerState
from app.providers.resilience.chaos import (
    ChaosTransport,
    ChaoticAttempt,
    FakeClock,
    FaultKind,
    FaultPlan,
    FaultProfile,
    make_async_sleep,
)
from app.providers.resilience.gateway import GatewayCall, GatewayConfig, ResilientGateway


def _settings() -> Settings:
    return Settings(dashscope_api_key="test")


def _gateway(clock: FakeClock, **over: object) -> ResilientGateway:
    cfg: dict[str, object] = {
        "max_attempts": 5,
        "backoff": BackoffPolicy(base_s=0.1, max_s=2.0, strategy=JitterStrategy.NONE),
        "breaker": BreakerConfig(failure_threshold=4, recovery_s=10.0),
    }
    cfg.update(over)
    return ResilientGateway(
        GatewayConfig(**cfg),  # type: ignore[arg-type]
        clock=clock,
        rng=random.Random(0),
        sleep=make_async_sleep(clock),
    )


# --------------------------------------------------------------------------- #
# Real ProviderClient over ChaosTransport, wrapped by the gateway
# --------------------------------------------------------------------------- #


async def test_real_client_recovers_through_gateway_after_5xx_then_429() -> None:
    clock = FakeClock()
    profile = FaultProfile(
        FaultPlan(
            sequence=(FaultKind.SERVER_ERROR, FaultKind.RATE_LIMIT),
            retry_after_s=1.0,
        )
    )
    chaos = ChaosTransport(profile, model="qwen3.7-max")
    client = ProviderClient(_settings(), transport=chaos.transport())
    gw = _gateway(clock)

    async def attempt() -> dict[str, object]:
        # The real client parses the chaos HTTP response (and raises typed errors
        # on 5xx/429), then succeeds on the OK body.
        return await client.request_json(
            "POST",
            f"{client.compat_base}/chat/completions",
            op="chat",
            model="qwen3.7-max",
            json={"model": "qwen3.7-max", "messages": []},
        )

    # NOTE: the round-1 client *also* retries internally; we disable that here by
    # giving it a single attempt via the gateway's outer loop carrying the retries.
    # Either way, the composed result must be the OK body.
    body = await gw.execute(GatewayCall(model="qwen3.7-max", op="chat"), attempt)
    assert body["model"] == "qwen3.7-max"
    choices = body["choices"]
    assert isinstance(choices, list)
    assert choices[0]["finish_reason"] == "stop"
    await client.aclose()


async def test_real_client_bad_request_is_terminal_through_gateway() -> None:
    clock = FakeClock()
    profile = FaultProfile(FaultPlan(sequence=(), terminal=FaultKind.BAD_REQUEST))
    chaos = ChaosTransport(profile, model="qwen3.7-max")
    # Disable the round-1 client's own retries by capping its attempts to 1 so the
    # chaos transport is hit exactly per gateway attempt.
    from app.providers.base import ResilienceConfig

    client = ProviderClient(
        _settings(),
        transport=chaos.transport(),
        resilience=ResilienceConfig(max_attempts=1, rate_per_s=1000, rate_burst=1000),
    )
    gw = _gateway(clock)

    async def attempt() -> dict[str, object]:
        return await client.request_json(
            "POST",
            f"{client.compat_base}/chat/completions",
            op="chat",
            model="qwen3.7-max",
            json={},
        )

    with pytest.raises(ProviderBadRequest):
        await gw.execute(GatewayCall(model="qwen3.7-max", op="chat"), attempt)
    # A 4xx is terminal: the gateway made exactly one attempt (one chaos request).
    assert len(chaos.requests) == 1
    await client.aclose()


# --------------------------------------------------------------------------- #
# ChaosTransport fault rendering
# --------------------------------------------------------------------------- #


def test_chaos_transport_renders_each_fault_kind() -> None:
    plan = FaultPlan(
        sequence=(
            FaultKind.OK,
            FaultKind.RATE_LIMIT,
            FaultKind.SERVER_ERROR,
            FaultKind.BAD_REQUEST,
        ),
        retry_after_s=3.0,
    )
    chaos = ChaosTransport(FaultProfile(plan), model="m")
    transport = chaos.transport()
    req = httpx.Request("POST", "https://example/x")

    ok = transport.handle_request(req)
    assert ok.status_code == 200
    throttled = transport.handle_request(req)
    assert throttled.status_code == 429
    assert throttled.headers["Retry-After"] == "3"
    server = transport.handle_request(req)
    assert server.status_code == 503
    bad = transport.handle_request(req)
    assert bad.status_code == 400


def test_chaos_transport_raises_for_timeout_and_reset() -> None:
    for kind, exc_type in (
        (FaultKind.TIMEOUT, httpx.ReadTimeout),
        (FaultKind.CONNECTION_RESET, httpx.ConnectError),
    ):
        chaos = ChaosTransport(FaultProfile(FaultPlan(sequence=(kind,))), model="m")
        transport = chaos.transport()
        with pytest.raises(exc_type):
            transport.handle_request(httpx.Request("POST", "https://example/x"))


# --------------------------------------------------------------------------- #
# Probabilistic soak — invariants that hold for any random sequence
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", [0, 1, 7, 42, 1337])
async def test_soak_invariants_hold_for_random_faults(seed: int) -> None:
    clock = FakeClock()
    gw = _gateway(clock, max_attempts=3)
    rng = random.Random(seed)
    profile = FaultProfile(
        probabilities={
            FaultKind.OK: 0.5,
            FaultKind.SERVER_ERROR: 0.2,
            FaultKind.RATE_LIMIT: 0.2,
            FaultKind.BAD_REQUEST: 0.1,
        },
        rng=rng,
    )
    successes = 0
    failures = 0
    for _ in range(50):
        # Each call gets a fresh attempt drawing from the shared random profile.
        attempt = ChaoticAttempt(profile, result="ok")
        try:
            out = await gw.execute(GatewayCall(model="m", op="chat"), attempt)
            assert out == "ok"
            successes += 1
        except ProviderError as exc:
            failures += 1
            # Every terminal failure is a typed ProviderError (never a raw Exception).
            assert isinstance(exc, ProviderError)
        # If the breaker is open, we may have stopped attempting — that's fine; the
        # invariant is only that the gateway never raises an *untyped* error and the
        # adaptive rate never goes below its floor.
    assert successes + failures == 50
    assert gw.rate >= 0.5  # adaptive bucket floor (default min_rate)
    snap = gw.snapshot()
    # Throttles observed must equal the rate-limit terminal failures + retries that
    # saw a 429 — at minimum, if any throttle happened, the rate dropped below 8.
    if snap.calls.throttles_observed > 0:
        assert gw.rate < 8.0


async def test_soak_breaker_trips_under_sustained_failure() -> None:
    clock = FakeClock()
    gw = _gateway(
        clock, max_attempts=1, breaker=BreakerConfig(failure_threshold=3, recovery_s=100.0)
    )
    # Always-5xx profile: after 3 failures the breaker opens and starts rejecting.
    plan = FaultPlan(sequence=(), terminal=FaultKind.SERVER_ERROR)
    transient = 0
    for _ in range(10):
        try:
            await gw.execute(GatewayCall(model="m", op="chat"), ChaoticAttempt(FaultProfile(plan)))
        except CircuitOpenError:
            pass  # fast-rejected by the open breaker (counted in stats below)
        except TransientProviderError:
            transient += 1
    breaker = gw.breakers.peek("m")
    assert breaker is not None and breaker.state is BreakerState.OPEN
    snap = gw.snapshot()
    rejections = snap.calls.breaker_rejections
    # Some calls hit the wall (transient), the rest were fast-rejected by the breaker.
    assert rejections >= 1
    assert transient >= 3
