"""Tests for jobs metrics emission (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime

from prometheus_client import generate_latest

from app.jobs import metrics
from app.jobs.harness import VirtualClockHarness
from app.jobs.registry import JobRegistry, job
from app.jobs.triggers import every, manual
from app.jobs.types import JobContext, JobResult
from app.observability.metrics import registry


def start() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def _scrape() -> str:
    return generate_latest(registry).decode("utf-8")


def test_metric_helpers_are_callable_and_safe() -> None:
    # All helpers must be no-throw even if a metric failed to register.
    metrics.inc_run("x", "completed")
    metrics.observe_duration("x", 0.1)
    metrics.inc_retry("x")
    metrics.inc_deadletter("x")
    metrics.set_active(3)
    metrics.set_leader(True)
    metrics.set_leader(False)


def test_metrics_appear_in_shared_registry() -> None:
    metrics.inc_run("metrictest", "completed")
    body = _scrape()
    assert "kinora_jobs_runs_total" in body
    assert "kinora_jobs_run_duration_seconds" in body
    assert "kinora_jobs_leader" in body


async def test_dispatch_emits_run_counter() -> None:
    reg = JobRegistry()

    @job("metrics.success", trigger=manual(), registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        return JobResult.ok()

    h = VirtualClockHarness(reg, start=start())
    await h.run_now("metrics.success")
    await h.drain_worker()
    body = _scrape()
    assert 'kinora_jobs_runs_total{decision="completed",job="metrics.success"}' in body


async def test_dispatch_emits_retry_and_deadletter() -> None:
    reg = JobRegistry()

    @job("metrics.fails", trigger=every(10), max_attempts=2, registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        raise RuntimeError("x")

    h = VirtualClockHarness(reg, start=start(), seed=0)
    await h.run_now("metrics.fails")
    for _ in range(4):
        await h.advance(300)
        await h.drain_worker()
    body = _scrape()
    assert "kinora_jobs_retries_total" in body
    assert 'job="metrics.fails"' in body
    assert "kinora_jobs_deadletters_total" in body
