"""Unit tests for the chaos-injection library (app.reliability.chaos).

Also includes a resilience test that drives the *real* RetryPolicy with a chaos
controller, proving the §12.1 retry→DLQ ladder behaves under injected faults.
"""

from __future__ import annotations

import pytest

from app.db.models.enums import RenderJobStatus
from app.queue.redis_queue import RetryDecision, RetryPolicy
from app.reliability.chaos import (
    SEAM_PROVIDER,
    SEAM_REDIS,
    ChaosController,
    FaultKind,
    FaultRule,
    InjectedFault,
    provider_rate_limit,
    provider_slow,
    redis_partition,
    transient_then_recover,
)


async def _ok() -> str:
    return "ok"


async def test_no_rules_calls_through() -> None:
    chaos = ChaosController(seed=1)
    result = await chaos.call(SEAM_PROVIDER, _ok)
    assert result == "ok"
    assert chaos.stats(SEAM_PROVIDER).calls == 1
    assert chaos.stats(SEAM_PROVIDER).errors_injected == 0


async def test_error_rule_raises_and_does_not_call_through() -> None:
    called = False

    async def _fn() -> str:
        nonlocal called
        called = True
        return "ok"

    chaos = ChaosController(seed=1)
    chaos.add_rule(SEAM_PROVIDER, provider_rate_limit())
    with pytest.raises(InjectedFault) as exc:
        await chaos.call(SEAM_PROVIDER, _fn)
    assert exc.value.seam == SEAM_PROVIDER
    assert "429" in str(exc.value)
    assert called is False  # the rule short-circuits the real call
    assert chaos.stats(SEAM_PROVIDER).errors_injected == 1


async def test_partition_rule_raises_partition() -> None:
    chaos = ChaosController(seed=1)
    chaos.add_rule(SEAM_REDIS, redis_partition())
    with pytest.raises(InjectedFault):
        await chaos.call(SEAM_REDIS, _ok)
    assert chaos.stats(SEAM_REDIS).partitions_injected == 1


async def test_latency_rule_injects_delay_via_sleep() -> None:
    slept: list[float] = []

    async def _sleep(s: float) -> None:
        slept.append(s)

    chaos = ChaosController(seed=1, sleep=_sleep)
    chaos.add_rule(SEAM_PROVIDER, provider_slow(latency_ms=250.0))
    result = await chaos.call(SEAM_PROVIDER, _ok)
    assert result == "ok"  # latency rules still call through
    assert slept == [0.25]
    assert chaos.stats(SEAM_PROVIDER).latency_injections == 1
    assert chaos.stats(SEAM_PROVIDER).total_injected_latency_ms == pytest.approx(250.0)


async def test_only_call_indices_targets_specific_calls() -> None:
    chaos = ChaosController(seed=1)
    chaos.add_rule(
        SEAM_PROVIDER,
        FaultRule(
            name="third_call",
            kind=FaultKind.ERROR,
            only_call_indices=frozenset({2}),
        ),
    )
    # Calls 0, 1 succeed; call 2 fails; call 3 succeeds.
    assert await chaos.call(SEAM_PROVIDER, _ok) == "ok"
    assert await chaos.call(SEAM_PROVIDER, _ok) == "ok"
    with pytest.raises(InjectedFault):
        await chaos.call(SEAM_PROVIDER, _ok)
    assert await chaos.call(SEAM_PROVIDER, _ok) == "ok"


async def test_probability_is_deterministic_under_seed() -> None:
    # Two identically-seeded controllers fire the same fault sequence.
    a = ChaosController(seed=42)
    b = ChaosController(seed=42)
    for c in (a, b):
        c.add_rule(SEAM_PROVIDER, FaultRule(name="flaky", kind=FaultKind.ERROR, probability=0.5))

    res_a: list[bool] = []
    res_b: list[bool] = []
    for _ in range(80):
        try:
            await a.call(SEAM_PROVIDER, _ok)
            res_a.append(True)
        except InjectedFault:
            res_a.append(False)
        try:
            await b.call(SEAM_PROVIDER, _ok)
            res_b.append(True)
        except InjectedFault:
            res_b.append(False)
    assert res_a == res_b
    # ~half fired.
    assert 0.3 < (res_a.count(False) / len(res_a)) < 0.7


async def test_wrap_forwards_args() -> None:
    async def _add(a: int, b: int) -> int:
        return a + b

    chaos = ChaosController(seed=1)
    wrapped = chaos.wrap(SEAM_PROVIDER, _add)
    assert await wrapped(2, 3) == 5
    assert chaos.stats(SEAM_PROVIDER).calls == 1


async def test_reset_restores_determinism() -> None:
    chaos = ChaosController(seed=7)
    chaos.add_rule(SEAM_PROVIDER, FaultRule(name="f", kind=FaultKind.ERROR, probability=0.5))

    async def take(n: int) -> list[bool]:
        out: list[bool] = []
        for _ in range(n):
            try:
                await chaos.call(SEAM_PROVIDER, _ok)
                out.append(True)
            except InjectedFault:
                out.append(False)
        return out

    first = await take(30)
    chaos.reset()
    assert chaos.stats(SEAM_PROVIDER).calls == 0
    second = await take(30)
    assert first == second


# --------------------------------------------------------------------------- #
# Resilience test: drive the real retry policy under injected faults (§12.1)
# --------------------------------------------------------------------------- #


async def test_retry_recovers_before_cap_under_transient_chaos() -> None:
    """A seam that fails its first 2 calls then recovers: the policy retries to
    success without dead-lettering (cap=2 means attempts 1 and 2 retry)."""
    chaos = transient_then_recover(SEAM_PROVIDER, fail_first=2)
    policy = RetryPolicy(cap=2, backoff_s=(2.0, 8.0, 30.0))

    attempts = 0
    decision = RetryDecision.RETRY
    status = RenderJobStatus.QUEUED
    while True:
        attempts_failed = False
        try:
            await chaos.call(SEAM_PROVIDER, _ok)
        except InjectedFault:
            attempts_failed = True
        if not attempts_failed:
            status = RenderJobStatus.SUCCEEDED
            break
        attempts += 1
        decision = policy.decide(attempts)
        if decision is RetryDecision.DEADLETTER:
            status = RenderJobStatus.DEADLETTER
            break

    # The 3rd call (index 2) succeeds, so the job lands SUCCEEDED after 2 retries.
    assert attempts == 2
    assert status is RenderJobStatus.SUCCEEDED
    assert chaos.stats(SEAM_PROVIDER).errors_injected == 2


async def test_retry_dead_letters_when_chaos_exceeds_cap() -> None:
    """A seam that fails its first 5 calls: the policy dead-letters past the cap,
    dropping the shot to degradation (§12.1/§12.4)."""
    chaos = transient_then_recover(SEAM_PROVIDER, fail_first=5)
    policy = RetryPolicy(cap=2, backoff_s=(2.0, 8.0, 30.0))

    attempts = 0
    status = RenderJobStatus.QUEUED
    while attempts < 10:
        try:
            await chaos.call(SEAM_PROVIDER, _ok)
            status = RenderJobStatus.SUCCEEDED
            break
        except InjectedFault:
            attempts += 1
            if policy.decide(attempts) is RetryDecision.DEADLETTER:
                status = RenderJobStatus.DEADLETTER
                break

    # cap=2 => attempt 3 dead-letters; the shot never blocks the pipeline.
    assert status is RenderJobStatus.DEADLETTER
    assert attempts == 3
