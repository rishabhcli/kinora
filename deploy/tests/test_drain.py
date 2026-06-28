"""Tests for drain + graceful shutdown coordination (offline, virtual clock)."""

from __future__ import annotations

from deploy.orchestrator.drain import DrainCoordinator, DrainPhase
from deploy.orchestrator.fakes import FakeRenderWorker, VirtualClock


async def test_clean_drain_finishes_inflight_then_terminates() -> None:
    clock = VirtualClock()
    worker = FakeRenderWorker(inflight_jobs=3, drain_rate=1)
    coord = DrainCoordinator(worker, now=clock, deadline_s=100.0)

    result = await coord.drain()

    assert worker.cordoned is True
    assert worker.terminated is True
    assert result.phase is DrainPhase.TERMINATED
    assert result.released == 0
    assert result.clean is True
    assert result.inflight_at_start == 3


async def test_idle_worker_drains_immediately() -> None:
    clock = VirtualClock()
    worker = FakeRenderWorker(inflight_jobs=0)
    result = await DrainCoordinator(worker, now=clock).drain()
    assert result.clean is True
    assert result.inflight_at_start == 0
    assert worker.terminated is True


async def test_stuck_worker_releases_jobs_at_deadline() -> None:
    # The clock advances via a metric-less helper: we advance time per poll by
    # making the deadline tiny and grace_polls 1 so it trips on first poll.
    clock = VirtualClock(start=0.0)
    worker = FakeRenderWorker(inflight_jobs=5, stuck=True)
    coord = DrainCoordinator(worker, now=clock, deadline_s=0.0, grace_polls=1000)

    result = await coord.drain()

    assert result.phase is DrainPhase.TIMED_OUT
    assert result.released == 5
    assert result.clean is False
    assert worker.terminated is True  # still terminates after release
    assert worker.released_total == 5


async def test_stuck_worker_caps_polls_even_if_clock_frozen() -> None:
    clock = VirtualClock(start=0.0)  # never advances
    worker = FakeRenderWorker(inflight_jobs=2, stuck=True)
    # deadline never reached (clock frozen), but grace_polls caps the loop.
    coord = DrainCoordinator(worker, now=clock, deadline_s=1e9, grace_polls=3)

    result = await coord.drain()

    assert result.phase is DrainPhase.TIMED_OUT
    assert result.polls == 3
    assert result.released == 2


async def test_drain_rate_finishes_in_multiple_polls() -> None:
    clock = VirtualClock()
    worker = FakeRenderWorker(inflight_jobs=6, drain_rate=2)
    result = await DrainCoordinator(worker, now=clock, deadline_s=100.0).drain()
    assert result.clean is True
    # 6 jobs at 2/poll → 3 polls to reach 0.
    assert result.polls == 3
