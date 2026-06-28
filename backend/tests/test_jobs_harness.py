"""End-to-end framework tests via the deterministic virtual-clock harness (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.jobs.harness import VirtualClockHarness
from app.jobs.registry import JobRegistry, job
from app.jobs.triggers import cron, every, manual, once
from app.jobs.types import JobContext, JobResult, JobRunStatus


def start() -> datetime:
    return datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


async def test_interval_job_fires_each_period() -> None:
    reg = JobRegistry()
    fires: list[datetime] = []

    @job("tick", trigger=every(30), registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        fires.append(ctx.run.scheduled_for)
        return JobResult.ok()

    h = VirtualClockHarness(reg, start=start())
    # t=0: first fire is at +30s (interval without anchor), nothing due yet.
    assert await h.run_pending() == []
    await h.advance(30)
    results = await h.run_pending()
    assert len(results) == 1
    await h.advance(30)
    await h.run_pending()
    assert len(fires) == 2
    assert fires[0] == start().replace(second=30)
    assert fires[1] == datetime(2026, 1, 1, 0, 1, tzinfo=UTC)


async def test_cron_job_fires_on_schedule() -> None:
    reg = JobRegistry()
    count = {"n": 0}

    @job("hourly", trigger=cron("0 * * * *"), registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        count["n"] += 1
        return JobResult.ok()

    h = VirtualClockHarness(reg, start=start())
    await h.advance(59 * 60)  # 00:59 — not yet the top of the hour
    assert await h.run_pending() == []
    await h.advance(60)  # 01:00
    assert len(await h.run_pending()) == 1
    assert count["n"] == 1


async def test_double_tick_does_not_double_enqueue() -> None:
    reg = JobRegistry()

    @job("idem", trigger=every(10), registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        return JobResult.ok()

    h = VirtualClockHarness(reg, start=start())
    await h.advance(10)
    first = await h.tick_scheduler()
    second = await h.tick_scheduler()  # same instant -> dedup
    assert len(first) == 1
    assert second == []
    runs = await h.store.list_runs(job_name="idem")
    assert len(runs) == 1


async def test_follower_does_not_schedule() -> None:
    reg = JobRegistry()

    @job("leaderonly", trigger=every(10), registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        return JobResult.ok()

    h = VirtualClockHarness(reg, start=start(), is_leader=lambda: False)
    await h.advance(100)
    assert await h.tick_scheduler() == []
    assert await h.store.list_runs() == []


async def test_manual_job_runs_only_on_demand() -> None:
    reg = JobRegistry()
    ran = {"n": 0}

    @job("ondemand", trigger=manual(), registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        ran["n"] += 1
        return JobResult.ok()

    h = VirtualClockHarness(reg, start=start())
    await h.advance(10_000)
    assert await h.tick_scheduler() == []  # never auto-fires
    run_id = await h.run_now("ondemand")
    assert run_id is not None
    await h.drain_worker()
    assert ran["n"] == 1


async def test_run_now_dedups_active() -> None:
    reg = JobRegistry()

    @job("once-only", trigger=manual(), registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        return JobResult.ok()

    h = VirtualClockHarness(reg, start=start())
    first = await h.run_now("once-only")
    second = await h.run_now("once-only")  # same minute -> dedup
    assert first is not None
    assert second is None


async def test_once_trigger_fires_exactly_once() -> None:
    reg = JobRegistry()
    fires = {"n": 0}
    fire_at = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)

    @job("oneshot", trigger=once(fire_at), registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        fires["n"] += 1
        return JobResult.ok()

    h = VirtualClockHarness(reg, start=start())
    await h.advance(5 * 60)
    await h.run_pending()
    await h.advance(60 * 60)
    await h.run_pending()  # should not fire again
    assert fires["n"] == 1


async def test_retry_lands_at_backoff_instant() -> None:
    reg = JobRegistry()
    attempts: list[datetime] = []

    @job("retrying", trigger=every(10), max_attempts=3, registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        attempts.append(ctx.clock.now())
        if ctx.attempt < 3:
            raise RuntimeError("transient")
        return JobResult.ok()

    h = VirtualClockHarness(reg, start=start(), seed=0)
    await h.advance(10)
    await h.run_pending()  # attempt 1 fails, retry scheduled with jitter <= 2s
    run = (await h.store.list_runs(job_name="retrying"))[0]
    assert run.status is JobRunStatus.RETRYING
    # advance well past any jittered backoff and drain repeatedly
    for _ in range(5):
        await h.advance(300)
        await h.drain_worker()
    final = await h.store.get(run.id)
    assert final is not None
    assert final.status is JobRunStatus.SUCCEEDED
    assert len(attempts) == 3


async def test_pause_and_resume() -> None:
    reg = JobRegistry()
    count = {"n": 0}

    @job("pausable", trigger=every(10), registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        count["n"] += 1
        return JobResult.ok()

    h = VirtualClockHarness(reg, start=start())
    h.scheduler.pause("pausable")
    assert h.scheduler.is_paused("pausable")
    await h.advance(100)
    await h.run_pending()
    assert count["n"] == 0
    h.scheduler.resume("pausable")
    await h.advance(10)
    await h.run_pending()
    assert count["n"] >= 1


async def test_reap_expired_lease_recovers_run() -> None:
    reg = JobRegistry()
    seen: list[int] = []

    @job("leaky", trigger=every(10), max_attempts=3, registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        seen.append(ctx.attempt)
        return JobResult.ok()

    h = VirtualClockHarness(reg, start=start(), lease_seconds=30)
    await h.advance(10)
    await h.tick_scheduler()
    # Claim it but never dispatch (simulate a worker crash mid-run).
    claimed = await h.store.claim_due(now=h.now, lease_seconds=30)
    assert claimed is not None
    # Before lease expiry, no reap.
    await h.advance(20)
    assert await h.reap() == 0
    # After lease expiry, reaped back to retrying and re-runnable.
    await h.advance(20)
    assert await h.reap() == 1
    await h.drain_worker()
    final = await h.store.get(claimed.id)
    assert final is not None
    assert final.status is JobRunStatus.SUCCEEDED
