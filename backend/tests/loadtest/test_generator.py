"""End-to-end generator runs (closed + open) on a VirtualClock + fake target."""

from __future__ import annotations

import pytest

from app.loadtest.arrival import ArrivalShape, RateEnvelope
from app.loadtest.clock import VirtualClock
from app.loadtest.generator import LoadGenerator, LoadModel, LoadPlan, RunResult
from app.loadtest.scenario import ReadEndpoint, Scenario, Step, ThinkShape, ThinkTime, steady_reader
from app.loadtest.target import FakeTarget, constant_service_time
from tests.loadtest.conftest import drive


def test_closed_loop_executes_full_journey_per_user() -> None:
    clock = VirtualClock()
    target = FakeTarget(clock, constant_service_time(0.05), seed=1)

    async def go() -> RunResult:
        gen = LoadGenerator(clock=clock, target=target)
        plan = LoadPlan(
            model=LoadModel.CLOSED,
            scenario=steady_reader(pages=3),
            users=4,
            iterations=2,
            seed=5,
        )
        return await gen.run(plan)

    result = drive(clock, go)
    # 4 users * 2 iterations * 11 requests/journey = 88.
    assert result.attempted == 88
    assert target.sent_count == 88
    assert result.collector.aggregate().counts.ok == 88


def test_closed_loop_think_time_advances_virtual_clock() -> None:
    clock = VirtualClock()
    target = FakeTarget(clock, constant_service_time(0.01), seed=1)

    sc = Scenario(
        name="two_steps",
        steps=[
            Step(ReadEndpoint.OPEN_BOOK, think=ThinkTime(10.0, ThinkShape.FIXED)),
            Step(ReadEndpoint.PAGE_TURN, think=ThinkTime(20.0, ThinkShape.FIXED)),
        ],
    )

    async def go() -> RunResult:
        gen = LoadGenerator(clock=clock, target=target)
        return await gen.run(
            LoadPlan(model=LoadModel.CLOSED, scenario=sc, users=1, iterations=1)
        )

    drive(clock, go)
    # 2 service (0.01 each) + 2 think (10 + 20) = 30.02 s of virtual time.
    assert clock.now() == pytest.approx(30.02, abs=0.001)


def test_open_loop_hits_target_rate() -> None:
    clock = VirtualClock()
    target = FakeTarget(clock, constant_service_time(0.01), seed=2)
    env = RateEnvelope(ArrivalShape.CONSTANT, duration_s=20.0, base_rate=15.0)

    async def go() -> RunResult:
        gen = LoadGenerator(clock=clock, target=target)
        plan = LoadPlan(
            model=LoadModel.OPEN,
            scenario=steady_reader(pages=2),
            envelope=env,
            poisson=False,  # deterministic placement for an exact count
            seed=3,
        )
        return await gen.run(plan)

    result = drive(clock, go)
    # 15 req/s * 20 s = 300 arrivals.
    assert result.attempted == 300
    assert result.collector.aggregate().counts.total == 300


def test_open_loop_backpressure_drops_over_inflight_cap() -> None:
    clock = VirtualClock()
    # Slow target (1 s service) + high arrival rate + tiny in-flight cap ⇒ drops.
    target = FakeTarget(clock, constant_service_time(1.0), seed=4)
    env = RateEnvelope(ArrivalShape.CONSTANT, duration_s=10.0, base_rate=20.0)

    async def go() -> RunResult:
        gen = LoadGenerator(clock=clock, target=target, max_inflight=3)
        plan = LoadPlan(
            model=LoadModel.OPEN,
            scenario=steady_reader(pages=2),
            envelope=env,
            poisson=False,
            seed=4,
        )
        return await gen.run(plan)

    result = drive(clock, go)
    assert result.dropped > 0  # backpressure shed load
    agg = result.collector.aggregate().counts
    assert agg.dropped == result.dropped
    # Every arrival is either really sent to the target or dropped. (The recorded
    # OK count can *exceed* real sends because omission backfill synthesizes the
    # slots a stall omitted — a correction, not an attempt — so we assert against
    # the target's real send tally, not the collected OK count.)
    assert result.attempted == target.sent_count + result.dropped


def test_timeout_reclassifies_slow_ok_as_timeout() -> None:
    clock = VirtualClock()
    target = FakeTarget(clock, constant_service_time(2.0), seed=1)

    sc = Scenario(name="one", steps=[Step(ReadEndpoint.OPEN_BOOK)])

    async def go() -> RunResult:
        gen = LoadGenerator(clock=clock, target=target)
        return await gen.run(
            LoadPlan(
                model=LoadModel.CLOSED,
                scenario=sc,
                users=1,
                iterations=1,
                timeout_s=0.5,  # 2 s service exceeds the 0.5 s deadline
            )
        )

    result = drive(clock, go)
    counts = result.collector.stats_for(ReadEndpoint.OPEN_BOOK).counts
    assert counts.timeout == 1
    assert counts.ok == 0


def test_errors_from_failing_endpoint_are_counted() -> None:
    clock = VirtualClock()
    target = FakeTarget(
        clock,
        constant_service_time(0.05),
        seed=1,
        failing_endpoints=frozenset({ReadEndpoint.JUMP}),
    )

    async def go() -> RunResult:
        gen = LoadGenerator(clock=clock, target=target)
        from app.loadtest.scenario import skimming_reader

        return await gen.run(
            LoadPlan(
                model=LoadModel.CLOSED,
                scenario=skimming_reader(jumps=4),
                users=2,
                iterations=1,
            )
        )

    result = drive(clock, go)
    jump = result.collector.stats_for(ReadEndpoint.JUMP).counts
    assert jump.error == 8  # 2 users * 4 jumps, all fail
    assert jump.ok == 0


def test_run_is_reproducible_for_seed() -> None:
    def run() -> tuple[int, float]:
        clock = VirtualClock()
        target = FakeTarget(clock, constant_service_time(0.03), seed=10)
        env = RateEnvelope(ArrivalShape.RAMP, duration_s=15.0, start_rate=2.0, end_rate=12.0)

        async def go() -> RunResult:
            gen = LoadGenerator(clock=clock, target=target)
            return await gen.run(
                LoadPlan(
                    model=LoadModel.OPEN,
                    scenario=steady_reader(pages=2),
                    envelope=env,
                    poisson=True,
                    seed=555,
                )
            )

        res = drive(clock, go)
        return res.attempted, round(res.collector.corrected_summary().p99, 6)

    assert run() == run()
