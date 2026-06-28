"""Lease guard + reaper tests (app.queue.leases, kinora.md §12.1).

Runs the real queue against the in-process fake; the lease heartbeat and the
reaper are driven with deterministic ``now_ms`` and a short heartbeat cadence.
"""

from __future__ import annotations

import asyncio

import pytest

from app.db.models.enums import RenderJobStatus, RenderPriority
from app.queue.fakeredis import FakeRedisClient
from app.queue.leases import LeaseGuard, Reaper
from app.queue.redis_queue import RedisRenderQueue

_BASE = 1_700_000_000_000


@pytest.fixture
def client() -> FakeRedisClient:
    return FakeRedisClient()


@pytest.fixture
def queue(client: FakeRedisClient) -> RedisRenderQueue:
    return RedisRenderQueue(client, namespace="kinora:test:lease", lease_ms=1000)


# --- LeaseGuard ------------------------------------------------------------- #


async def test_lease_guard_heartbeats_during_block(queue: RedisRenderQueue) -> None:
    await queue.enqueue(
        shot_hash="h", priority=RenderPriority.COMMITTED, book_id="b", job_id="j", now_ms=_BASE
    )
    claimed = await queue.claim(now_ms=_BASE)
    assert claimed is not None
    async with LeaseGuard(queue, "j", heartbeat_s=0.02) as guard:
        await asyncio.sleep(0.1)  # several heartbeat cadences
    assert guard.beats >= 1


async def test_lease_guard_stops_cleanly_on_exit(queue: RedisRenderQueue) -> None:
    await queue.enqueue(
        shot_hash="h", priority=RenderPriority.COMMITTED, book_id="b", job_id="j", now_ms=_BASE
    )
    await queue.claim(now_ms=_BASE)
    guard = LeaseGuard(queue, "j", heartbeat_s=0.02)
    async with guard:
        pass  # immediate exit
    beats_at_exit = guard.beats
    await asyncio.sleep(0.05)  # no further beats after the block
    assert guard.beats == beats_at_exit


async def test_lease_guard_noop_for_unleased_job(queue: RedisRenderQueue) -> None:
    # No claim -> extend_lease returns False -> guard records no beats but never errors.
    async with LeaseGuard(queue, "ghost", heartbeat_s=0.02) as guard:
        await asyncio.sleep(0.06)
    assert guard.beats == 0


# --- Reaper ----------------------------------------------------------------- #


async def test_reaper_run_once_reclaims_expired(queue: RedisRenderQueue) -> None:
    await queue.enqueue(
        shot_hash="h", priority=RenderPriority.COMMITTED, book_id="b", job_id="j", now_ms=_BASE
    )
    await queue.claim(now_ms=_BASE)  # leased until _BASE + 1000
    reaper = Reaper(queue)
    assert await reaper.run_once(now_ms=_BASE + 500) == 0  # still leased
    reclaimed = await reaper.run_once(now_ms=_BASE + 2000)  # lease lapsed
    assert reclaimed == 1
    assert reaper.total_reaped == 1
    job = await queue.get_job("j")
    assert job is not None and job.status is RenderJobStatus.QUEUED


async def test_reaper_loop_stops_on_event(queue: RedisRenderQueue) -> None:
    reaper = Reaper(queue, interval_s=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(reaper.run(stop=stop))
    await asyncio.sleep(0.03)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()
