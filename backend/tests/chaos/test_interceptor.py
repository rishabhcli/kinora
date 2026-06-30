"""Deterministic tests for the in-process fault injector + blast-radius scoping."""

from __future__ import annotations

import contextlib

import pytest

from app.chaos.clock import VirtualClock
from app.chaos.faults import (
    DependencyDownFault,
    ErrorFault,
    InjectedConnectionError,
    InjectedFault,
    LatencyFault,
    PartialResponseFault,
    RateLimitStormFault,
    TimeoutFault,
)
from app.chaos.interceptor import FaultInjector


async def _ok() -> str:
    return "real-result"


async def test_clean_call_passes_through_and_records_timeline() -> None:
    clock = VirtualClock(start=0.0)
    inj = FaultInjector(seed=0, clock=clock)
    result = await inj.call("redis", _ok)
    assert result == "real-result"
    assert len(inj.timeline) == 1
    assert inj.timeline[0].fault_name is None
    assert inj.timeline[0].raised is None


async def test_error_fault_raises_and_records() -> None:
    clock = VirtualClock(start=0.0)
    inj = FaultInjector(seed=0, clock=clock)
    inj.arm(ErrorFault(dependency="dashscope", name="boom", message="x"))
    with pytest.raises(InjectedFault):
        await inj.call("dashscope", _ok)
    rec = inj.timeline[-1]
    assert rec.fault_name == "boom"
    assert rec.raised == "InjectedFault"


async def test_latency_fault_advances_virtual_clock_only() -> None:
    clock = VirtualClock(start=0.0)
    inj = FaultInjector(seed=0, clock=clock)
    inj.arm(LatencyFault(dependency="pg", name="slow", base_latency_s=2.0))
    result = await inj.call("pg", _ok)
    assert result == "real-result"
    assert clock.time() == 2.0  # virtual time advanced, no real wait
    assert inj.timeline[-1].delay_s == 2.0


async def test_partial_response_transforms_real_result() -> None:
    clock = VirtualClock(start=0.0)
    inj = FaultInjector(seed=0, clock=clock)
    inj.arm(PartialResponseFault(dependency="object_store", name="short", keep_fraction=0.5))

    async def _body() -> str:
        return "abcdef"

    out = await inj.call("object_store", _body)
    assert out == "abc"


async def test_timeout_fault_delays_then_raises() -> None:
    clock = VirtualClock(start=0.0)
    inj = FaultInjector(seed=0, clock=clock)
    inj.arm(TimeoutFault(dependency="pg", name="hang", deadline_s=5.0))
    with pytest.raises(InjectedFault):
        await inj.call("pg", _ok)
    assert clock.time() == 5.0  # spent the deadline waiting


async def test_blast_radius_scoping_protects_out_of_scope_deps() -> None:
    clock = VirtualClock(start=0.0)
    inj = FaultInjector(seed=0, clock=clock, scope={"dashscope"})
    # Fault armed for redis, but redis is outside scope → call passes through.
    inj.arm(DependencyDownFault(dependency="redis", name="down"))
    result = await inj.call("redis", _ok)
    assert result == "real-result"
    assert not inj.in_scope("redis")
    # In-scope dashscope IS faulted.
    inj.arm(DependencyDownFault(dependency="dashscope", name="down2"))
    with pytest.raises(InjectedConnectionError):
        await inj.call("dashscope", _ok)


async def test_call_index_is_per_dependency_and_deterministic() -> None:
    clock = VirtualClock(start=0.0)
    inj = FaultInjector(seed=0, clock=clock)
    inj.arm(RateLimitStormFault(dependency="dashscope", name="storm", allow_first=2))
    # First two dashscope calls succeed.
    assert await inj.call("dashscope", _ok) == "real-result"
    assert await inj.call("dashscope", _ok) == "real-result"
    # Third 429s.
    with pytest.raises(InjectedFault):
        await inj.call("dashscope", _ok)
    # A different dependency keeps its own index (unaffected).
    assert await inj.call("redis", _ok) == "real-result"


async def test_disarm_all_rolls_back() -> None:
    clock = VirtualClock(start=0.0)
    inj = FaultInjector(seed=0, clock=clock)
    inj.arm(DependencyDownFault(dependency="redis", name="down"))
    assert inj.armed_dependencies == {"redis"}
    inj.disarm_all()
    assert inj.armed_dependencies == set()
    # After rollback, calls succeed again.
    assert await inj.call("redis", _ok) == "real-result"


async def test_wrap_returns_drop_in_async_callable() -> None:
    clock = VirtualClock(start=0.0)
    inj = FaultInjector(seed=0, clock=clock)
    wrapped = inj.wrap("redis", _ok)
    assert await wrapped() == "real-result"
    assert len(inj.timeline) == 1


async def test_determinism_same_seed_same_sequence() -> None:
    async def run(seed: int) -> list[str]:
        clock = VirtualClock(start=0.0)
        inj = FaultInjector(seed=seed, clock=clock)
        inj.arm(LatencyFault(dependency="d", name="n", probability=0.5, base_latency_s=1.0))
        for _ in range(10):
            with contextlib.suppress(InjectedFault):
                await inj.call("d", _ok)
        return [r.effect_label for r in inj.timeline]

    a = await run(99)
    b = await run(99)
    assert a == b
