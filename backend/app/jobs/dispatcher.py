"""The dispatcher — run one claimed job to a terminal/retry state.

Given a claimed :class:`~app.jobs.types.JobRun`, the dispatcher:

1. looks the handler up in the registry (a missing handler dead-letters the run —
   the code was removed but a durable run survived);
2. builds a :class:`~app.jobs.types.JobContext` (run + attempt + clock + logger +
   injected resources) and invokes the handler;
3. interprets the result:
   * ``SUCCESS`` / ``SKIPPED`` (or a bare ``None``) -> :meth:`JobStore.complete`,
   * ``FAILED`` or a raised exception -> the **retry/DLQ** path: ask the job's
     :class:`~app.jobs.backoff.BackoffPolicy` whether to retry (compute the
     jittered ``available_at`` and re-queue) or dead-letter (capture the error).

This is the single place at-least-once execution semantics live; the worker just
claims and hands runs here. Handlers are expected to be idempotent — the
framework guarantees no *duplicate enqueue*, but a crash between handler success
and the store write can re-run a handler, so handlers must tolerate that.

Everything is defensive: a handler that raises never escapes the dispatcher (it
becomes a retry/DLQ), and a store write failure is logged, never propagated to
crash the worker loop.
"""

from __future__ import annotations

import random
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.core.logging import get_logger
from app.jobs import metrics
from app.jobs.backoff import RetryDecision
from app.jobs.clock import Clock, SystemClock
from app.jobs.registry import JobRegistry
from app.jobs.store import JobStore
from app.jobs.types import JobContext, JobResult, JobRun, RunOutcome

logger = get_logger("app.jobs.dispatcher")


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """What the dispatcher decided for a run (for metrics / tests)."""

    run_id: str
    job_name: str
    outcome: RunOutcome
    decision: str  # "completed" | "retry" | "deadletter"
    attempt: int
    delay_s: float = 0.0


class Dispatcher:
    """Execute claimed runs with at-least-once, retried-and-dead-lettered semantics."""

    def __init__(
        self,
        *,
        registry: JobRegistry,
        store: JobStore,
        clock: Clock | None = None,
        resources: Mapping[str, Any] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self._clock = clock or SystemClock()
        self._resources = dict(resources or {})
        self._rng = rng

    async def dispatch(self, run: JobRun) -> DispatchResult:
        """Run ``run``'s handler and drive the store to the next state."""
        definition = self._registry.get(run.job_name)
        if definition is None:
            error = f"no handler registered for job {run.job_name!r}"
            logger.warning("jobs.dispatch.unregistered", run_id=run.id, job=run.job_name)
            await self._safe_deadletter(run, error)
            metrics.inc_run(run.job_name, "deadletter")
            metrics.inc_deadletter(run.job_name)
            return DispatchResult(
                run_id=run.id,
                job_name=run.job_name,
                outcome=RunOutcome.FAILED,
                decision="deadletter",
                attempt=run.attempt,
            )

        ctx = JobContext(
            run=run,
            attempt=run.attempt,
            clock=self._clock,
            logger=logger.bind(job=run.job_name, run_id=run.id, attempt=run.attempt),
            resources=self._resources,
        )

        started = time.monotonic()
        try:
            raw = await definition.handler(ctx)
        except Exception as exc:  # noqa: BLE001 - handler errors become retry/DLQ
            metrics.observe_duration(run.job_name, time.monotonic() - started)
            logger.warning(
                "jobs.dispatch.handler_error",
                run_id=run.id,
                job=run.job_name,
                attempt=run.attempt,
                error=str(exc),
            )
            return await self._handle_failure(run, error=f"{type(exc).__name__}: {exc}")
        metrics.observe_duration(run.job_name, time.monotonic() - started)

        result = raw if isinstance(raw, JobResult) else JobResult.ok()
        if result.is_failure:
            return await self._handle_failure(
                run, error=str(result.detail.get("error", "handler reported failure"))
            )

        await self._safe(
            self._store.complete(run.id, outcome=result.outcome, detail=result.detail),
            "complete",
            run.id,
        )
        metrics.inc_run(run.job_name, "completed")
        logger.info(
            "jobs.dispatch.completed",
            run_id=run.id,
            job=run.job_name,
            outcome=result.outcome.value,
            attempt=run.attempt,
        )
        return DispatchResult(
            run_id=run.id,
            job_name=run.job_name,
            outcome=result.outcome,
            decision="completed",
            attempt=run.attempt,
        )

    async def _handle_failure(self, run: JobRun, *, error: str) -> DispatchResult:
        definition = self._registry.get(run.job_name)
        policy = definition.backoff if definition is not None else None
        # ``run.attempt`` is the attempt that just failed (1-based, set on claim).
        if policy is None or policy.decide(run.attempt) is RetryDecision.DEADLETTER:
            await self._safe_deadletter(run, error)
            metrics.inc_run(run.job_name, "deadletter")
            metrics.inc_deadletter(run.job_name)
            logger.warning(
                "jobs.dispatch.deadletter", run_id=run.id, job=run.job_name, attempt=run.attempt
            )
            return DispatchResult(
                run_id=run.id,
                job_name=run.job_name,
                outcome=RunOutcome.FAILED,
                decision="deadletter",
                attempt=run.attempt,
            )

        delay_s = policy.delay_for(run.attempt + 1, rng=self._rng)
        available_at = self._clock.now() + timedelta(seconds=delay_s)
        await self._safe(
            self._store.retry(run.id, available_at=available_at, error=error), "retry", run.id
        )
        metrics.inc_run(run.job_name, "retry")
        metrics.inc_retry(run.job_name)
        logger.info(
            "jobs.dispatch.retry",
            run_id=run.id,
            job=run.job_name,
            attempt=run.attempt,
            delay_s=round(delay_s, 3),
        )
        return DispatchResult(
            run_id=run.id,
            job_name=run.job_name,
            outcome=RunOutcome.FAILED,
            decision="retry",
            attempt=run.attempt,
            delay_s=delay_s,
        )

    async def _safe_deadletter(self, run: JobRun, error: str) -> None:
        await self._safe(self._store.deadletter(run.id, error=error), "deadletter", run.id)

    @staticmethod
    async def _safe(coro: Any, op: str, run_id: str) -> None:
        try:
            await coro
        except Exception as exc:  # noqa: BLE001 - store write must not crash the worker
            logger.error("jobs.dispatch.store_write_failed", op=op, run_id=run_id, error=str(exc))


__all__ = ["DispatchResult", "Dispatcher"]
