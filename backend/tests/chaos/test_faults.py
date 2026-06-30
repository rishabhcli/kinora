"""Deterministic tests for each fault kind's effect (seeded RNG, no infra)."""

from __future__ import annotations

import random

from app.chaos.faults import (
    ClockSkewFault,
    ConnectionDropFault,
    DependencyDownFault,
    ErrorFault,
    FaultContext,
    FaultKind,
    InjectedConnectionError,
    InjectedFault,
    InjectedRateLimit,
    InjectedTimeout,
    LatencyFault,
    PartialResponseFault,
    RateLimitStormFault,
    TimeoutFault,
)


def _ctx(*, call_index: int = 0, seed: int = 0, dep: str = "dep") -> FaultContext:
    return FaultContext(
        dependency=dep,
        call_index=call_index,
        rng=random.Random(seed),
        now=0.0,
        armed_at=0.0,
    )


def test_latency_fault_adds_fixed_delay() -> None:
    fault = LatencyFault(dependency="pg", name="slow", base_latency_s=0.5)
    effect = fault.apply(_ctx())
    assert fault.kind is FaultKind.LATENCY
    assert effect.delay_s == 0.5
    assert effect.raises is None
    assert not effect.is_passthrough


def test_latency_fault_jitter_is_deterministic() -> None:
    fault = LatencyFault(dependency="pg", name="slow", base_latency_s=0.5, jitter_s=0.5)
    e1 = fault.apply(_ctx(seed=42))
    e2 = fault.apply(_ctx(seed=42))
    assert e1.delay_s == e2.delay_s  # same seed → same jitter
    assert 0.5 <= e1.delay_s <= 1.0


def test_error_fault_raises_injected_error() -> None:
    fault = ErrorFault(dependency="dashscope", name="boom", message="kaboom")
    effect = fault.apply(_ctx())
    assert isinstance(effect.raises, InjectedFault)
    assert effect.raises.dependency == "dashscope"
    assert "kaboom" in str(effect.raises)


def test_timeout_fault_delays_then_raises_timeout() -> None:
    fault = TimeoutFault(dependency="pg", name="hang", deadline_s=3.0)
    effect = fault.apply(_ctx())
    assert effect.delay_s == 3.0
    assert isinstance(effect.raises, InjectedTimeout)


def test_connection_drop_raises_connection_error_no_delay() -> None:
    fault = ConnectionDropFault(dependency="redis", name="reset")
    effect = fault.apply(_ctx())
    assert effect.delay_s == 0.0
    assert isinstance(effect.raises, InjectedConnectionError)


def test_partial_response_truncates_result() -> None:
    fault = PartialResponseFault(dependency="object_store", name="short", keep_fraction=0.5)
    effect = fault.apply(_ctx())
    assert effect.transform is not None
    assert effect.raises is None
    assert effect.transform("abcdef") == "abc"
    assert effect.transform([1, 2, 3, 4]) == [1, 2]
    assert effect.transform({"a": 1, "b": 2, "c": 3, "d": 4}) == {"a": 1, "b": 2}


def test_partial_response_custom_transform() -> None:
    fault = PartialResponseFault(
        dependency="object_store", name="corrupt", transform_fn=lambda r: None
    )
    effect = fault.apply(_ctx())
    assert effect.transform is not None
    assert effect.transform("anything") is None


def test_clock_skew_surfaces_skew_no_raise() -> None:
    fault = ClockSkewFault(dependency="dashscope", name="skew", skew_s_value=120.0)
    effect = fault.apply(_ctx())
    assert effect.skew_s == 120.0
    assert effect.raises is None
    assert effect.delay_s == 0.0
    assert not effect.is_passthrough


def test_dependency_down_always_raises() -> None:
    fault = DependencyDownFault(dependency="redis", name="down")
    for i in range(5):
        effect = fault.apply(_ctx(call_index=i))
        assert isinstance(effect.raises, InjectedConnectionError)


def test_rate_limit_storm_allows_first_then_429s() -> None:
    fault = RateLimitStormFault(dependency="dashscope", name="storm", allow_first=3)
    # First 3 calls pass through.
    for i in range(3):
        effect = fault.apply(_ctx(call_index=i))
        assert effect.is_passthrough
    # Subsequent calls 429.
    for i in range(3, 6):
        effect = fault.apply(_ctx(call_index=i))
        assert isinstance(effect.raises, InjectedRateLimit)
        assert effect.raises.retry_after_s == 2.0


def test_probability_gate_is_seeded() -> None:
    # probability 0 → never fires regardless of call.
    never = ErrorFault(dependency="d", name="n", probability=0.0)
    assert never.apply(_ctx(seed=1)).is_passthrough
    # probability 1 → always fires.
    always = ErrorFault(dependency="d", name="n", probability=1.0)
    assert always.apply(_ctx(seed=1)).raises is not None
    # A fractional probability is deterministic under a fixed seed.
    half = LatencyFault(dependency="d", name="n", probability=0.5, base_latency_s=1.0)
    seq1 = [half.apply(_ctx(seed=7, call_index=i)).is_passthrough for i in range(10)]
    half2 = LatencyFault(dependency="d", name="n", probability=0.5, base_latency_s=1.0)
    seq2 = [half2.apply(_ctx(seed=7, call_index=i)).is_passthrough for i in range(10)]
    assert seq1 == seq2
