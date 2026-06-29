"""Tests for the circuit breaker state machine."""

from __future__ import annotations

from app.distributed.rpc.circuit import (
    BreakerState,
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitConfig,
)
from app.distributed.rpc.deadline import ManualClock
from app.distributed.rpc.errors import FailureKind, RpcError, RpcStatus, not_found, unavailable


def _cfg(**kw: object) -> CircuitConfig:
    base: dict[str, object] = {
        "failure_threshold": 0.5,
        "min_samples": 4,
        "window_size": 10,
        "reset_timeout_s": 5.0,
    }
    base.update(kw)
    return CircuitConfig(**base)  # type: ignore[arg-type]


def test_starts_closed_and_allows() -> None:
    clk = ManualClock()
    b = CircuitBreaker(config=_cfg(), name="svc.m")
    assert b.state is BreakerState.CLOSED
    assert b.allow(clock=clk)


def test_opens_after_threshold_breached() -> None:
    clk = ManualClock()
    b = CircuitBreaker(config=_cfg(min_samples=4, failure_threshold=0.5), name="svc.m")
    # 2 ok, then 2 fail across a 4-sample window → 50% ratio → open.
    b.record(success=True, clock=clk)
    b.record(success=True, clock=clk)
    b.record(success=False, clock=clk)
    assert b.state is BreakerState.CLOSED  # only 3 samples
    b.record(success=False, clock=clk)
    assert b.state is BreakerState.OPEN
    assert b.opened_count == 1


def test_open_rejects_until_reset_timeout() -> None:
    clk = ManualClock()
    b = CircuitBreaker(config=_cfg(min_samples=2, failure_threshold=0.5, reset_timeout_s=5.0))
    b.record(success=False, clock=clk)
    b.record(success=False, clock=clk)
    assert b.state is BreakerState.OPEN
    assert not b.allow(clock=clk)
    assert b.rejected_count == 1
    # Before timeout: still rejects.
    clk.advance(4.9)
    assert not b.allow(clock=clk)
    # After timeout: promotes to half-open and admits a probe.
    clk.advance(0.2)
    assert b.allow(clock=clk)
    assert b.state is BreakerState.HALF_OPEN


def test_half_open_closes_after_successes() -> None:
    clk = ManualClock()
    b = CircuitBreaker(
        config=_cfg(
            min_samples=2,
            failure_threshold=0.5,
            reset_timeout_s=1.0,
            half_open_max_calls=3,
            half_open_successes=2,
        )
    )
    b.record(success=False, clock=clk)
    b.record(success=False, clock=clk)
    clk.advance(1.1)
    assert b.allow(clock=clk)  # half-open probe 1
    b.record(success=True, clock=clk)
    assert b.allow(clock=clk)  # probe 2
    b.record(success=True, clock=clk)
    assert b.state is BreakerState.CLOSED


def test_half_open_single_failure_reopens() -> None:
    clk = ManualClock()
    b = CircuitBreaker(config=_cfg(min_samples=2, failure_threshold=0.5, reset_timeout_s=1.0))
    b.record(success=False, clock=clk)
    b.record(success=False, clock=clk)
    clk.advance(1.1)
    assert b.allow(clock=clk)
    b.record(success=False, clock=clk)  # probe fails
    assert b.state is BreakerState.OPEN
    assert b.opened_count == 2


def test_half_open_limits_concurrent_probes() -> None:
    clk = ManualClock()
    cfg = _cfg(
        min_samples=2, failure_threshold=0.5, reset_timeout_s=1.0, half_open_max_calls=2
    )
    b = CircuitBreaker(config=cfg)
    b.record(success=False, clock=clk)
    b.record(success=False, clock=clk)
    clk.advance(1.1)
    assert b.allow(clock=clk)  # probe 1
    assert b.allow(clock=clk)  # probe 2
    assert not b.allow(clock=clk)  # 3rd rejected while 2 in flight


def test_counts_against_breaker_classification() -> None:
    b = CircuitBreaker()
    transport = unavailable("down")
    assert b.counts_against_breaker(transport)
    # An application NOT_FOUND from a healthy server does NOT count.
    assert not b.counts_against_breaker(not_found("absent"))
    # An application-kind INTERNAL still counts (server-side distress).
    internal_app = RpcError(RpcStatus.INTERNAL, "boom", kind=FailureKind.APPLICATION)
    assert b.counts_against_breaker(internal_app)


def test_reject_error_is_transport_unavailable() -> None:
    b = CircuitBreaker(name="memory.read")
    err = b.reject_error()
    assert err.status is RpcStatus.UNAVAILABLE
    assert err.is_transport


def test_registry_lazily_creates_per_endpoint() -> None:
    reg = CircuitBreakerRegistry(default_config=_cfg())
    a = reg.get("svc.a")
    b = reg.get("svc.b")
    assert a is not b
    assert reg.get("svc.a") is a  # same instance on re-get
    assert set(reg.states()) == {"svc.a", "svc.b"}
