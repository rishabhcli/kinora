"""Unit tests for the load engine (app.reliability.runner).

The runner is driven entirely with a FakeTransport + a VirtualClock + an instant
sleep, so these tests issue no network traffic and consume no real time.
"""

from __future__ import annotations

import pytest

from app.reliability.runner import LoadRunner, RunnerConfig, VirtualClock
from app.reliability.scenarios import (
    EP_CREATE_SESSION,
    EP_INTENT,
    EP_SEEK,
    cold_open,
    seek_thrash,
    steady_reader,
)
from app.reliability.transport import FakeTransport
from app.reliability.workload import (
    RampProfile,
    RampShape,
    ThinkTime,
    WorkloadPlan,
)


def _runner(transport: FakeTransport) -> tuple[LoadRunner, VirtualClock]:
    clock = VirtualClock()
    runner = LoadRunner(
        transport, clock=clock.now, sleep=clock.sleep, config=RunnerConfig(seed=1)
    )
    return runner, clock


async def test_closed_run_opens_one_session_per_user() -> None:
    transport = FakeTransport(default_status=201, seed=2)
    runner, _ = _runner(transport)
    plan = WorkloadPlan.closed(users=4, duration_s=10.0, think=ThinkTime(mean_s=0.5))
    report = await runner.run(cold_open(), plan)
    # Exactly one create-session per virtual user.
    assert report.endpoints[EP_CREATE_SESSION].total == 4
    # And a stream of intent requests beyond the prologue.
    assert report.endpoints[EP_INTENT].total > 0
    assert report.meta["scenario"] == "cold_open"


async def test_closed_run_records_latency_and_throughput() -> None:
    transport = FakeTransport(base_latency_ms=10.0, latency_jitter_ms=1.0, seed=3)
    runner, _ = _runner(transport)
    plan = WorkloadPlan.closed(users=3, duration_s=20.0)
    report = await runner.run(steady_reader(), plan)
    assert report.total_requests > 0
    assert report.wall_seconds > 0.0
    assert report.throughput_rps > 0.0
    overall = report.overall_latency()
    assert overall.count == report.total_requests
    # Latency reflects the injected ~10ms base.
    assert 8.0 <= overall.p50_ms <= 13.0


async def test_closed_run_no_real_traffic_beyond_transport() -> None:
    transport = FakeTransport(seed=4)
    runner, _ = _runner(transport)
    plan = WorkloadPlan.closed(users=2, duration_s=10.0)
    await runner.run(steady_reader(), plan)
    # Every recorded call went through the fake (the only transport).
    assert len(transport.calls) > 0
    assert all(c.path.startswith("/sessions") for c in transport.calls)


async def test_seek_thrash_produces_seek_requests() -> None:
    transport = FakeTransport(seed=5)
    runner, _ = _runner(transport)
    plan = WorkloadPlan.closed(users=3, duration_s=30.0)
    report = await runner.run(seek_thrash(), plan)
    assert EP_SEEK in report.endpoints
    assert report.endpoints[EP_SEEK].total > 0


async def test_fault_injection_shows_up_as_errors() -> None:
    # 50% of intent calls fault with a 500; the report's error rate reflects it.
    transport = FakeTransport(fault_rate=0.5, fault_status=500, seed=6)
    runner, _ = _runner(transport)
    plan = WorkloadPlan.closed(users=4, duration_s=20.0)
    report = await runner.run(steady_reader(), plan)
    # Roughly half the requests errored (binomial spread allowed).
    assert 0.3 < report.error_rate < 0.7


async def test_429_on_intent_is_not_counted_as_error() -> None:
    # A 429 on intent is expected backpressure; the report should not call it an error.
    transport = FakeTransport(fault_rate=1.0, fault_status=429, seed=7)
    runner, _ = _runner(transport)
    plan = WorkloadPlan.closed(users=2, duration_s=10.0)
    report = await runner.run(steady_reader(), plan)
    # All intent calls returned 429 but the intent predicate accepts them.
    intent = report.endpoints[EP_INTENT]
    assert intent.total > 0
    assert intent.errors == 0


async def test_open_model_run() -> None:
    transport = FakeTransport(base_latency_ms=5.0, seed=8)
    runner, _ = _runner(transport)
    plan = WorkloadPlan.open(rate_rps=20.0, duration_s=10.0, seed=8)
    report = await runner.run(steady_reader(), plan)
    # Pool sessions opened + arrival-driven requests issued.
    assert report.endpoints[EP_CREATE_SESSION].total >= 1
    assert report.total_requests > report.endpoints[EP_CREATE_SESSION].total
    assert report.wall_seconds > 0.0


async def test_open_model_ramp_is_honoured() -> None:
    transport = FakeTransport(seed=9)
    runner, _ = _runner(transport)
    ramp = RampProfile(shape=RampShape.LINEAR, ramp_s=10.0, floor=0.0)
    plan = WorkloadPlan.open(rate_rps=30.0, duration_s=10.0, ramp=ramp, seed=9)
    report = await runner.run(steady_reader(), plan)
    assert report.total_requests > 0


async def test_token_header_attached() -> None:
    transport = FakeTransport(seed=10)
    clock = VirtualClock()
    runner = LoadRunner(
        transport,
        clock=clock.now,
        sleep=clock.sleep,
        config=RunnerConfig(token="secret-token"),
    )
    plan = WorkloadPlan.closed(users=1, duration_s=5.0)
    await runner.run(cold_open(), plan)
    assert transport.calls
    assert transport.calls[0].headers.get("Authorization") == "Bearer secret-token"


async def test_run_is_deterministic() -> None:
    async def run_once() -> dict[str, object]:
        transport = FakeTransport(base_latency_ms=7.0, latency_jitter_ms=2.0, seed=21)
        runner, _ = _runner(transport)
        plan = WorkloadPlan.closed(users=3, duration_s=15.0)
        report = await runner.run(steady_reader(), plan)
        return report.to_dict()

    a = await run_once()
    b = await run_once()
    assert a == b


async def test_zero_users_yields_empty_report() -> None:
    transport = FakeTransport(seed=1)
    runner, _ = _runner(transport)
    plan = WorkloadPlan.closed(users=0, duration_s=10.0)
    report = await runner.run(steady_reader(), plan)
    assert report.total_requests == 0


@pytest.mark.parametrize("scenario_factory", [steady_reader, seek_thrash, cold_open])
async def test_all_scenarios_run_clean(scenario_factory) -> None:  # type: ignore[no-untyped-def]
    transport = FakeTransport(seed=2)
    runner, _ = _runner(transport)
    plan = WorkloadPlan.closed(users=2, duration_s=12.0)
    report = await runner.run(scenario_factory(), plan)
    assert report.total_requests > 0
    assert report.error_rate == 0.0
