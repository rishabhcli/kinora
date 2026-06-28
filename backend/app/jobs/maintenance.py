"""Built-in maintenance jobs — *registrations only*, no-op-safe by design.

These are the operational cadences the rest of Kinora benefits from running in the
background. Each is a small handler registered against a :class:`JobRegistry`; the
handler resolves its target subsystem **from the injected resource bag at run
time** and returns :meth:`JobResult.skipped` when that subsystem isn't present. So:

* importing this module and registering the jobs never *forces* the digest /
  search / GC / recovery / budget subsystems to exist — a node that didn't inject
  them simply runs jobs that cleanly skip;
* when a resource *is* injected (by an entrypoint via
  :func:`~app.jobs.service.build_job_service`), the job does real work.

The five jobs and their resource keys:

* ``maintenance.digest_flush`` (resource ``digest_flusher``) — flush batched
  notification/event digests;
* ``maintenance.search_index_refresh`` (``search_indexer``) — refresh/rebuild
  the search index;
* ``maintenance.retention_gc`` (``retention_gc``) — sweep expired/orphaned
  artifacts;
* ``maintenance.stuck_import_recovery`` (``import_recovery``) — re-drive books
  stuck ``importing`` (a §4.7 cadence sibling);
* ``maintenance.budget_reconcile`` (``budget_reconciler``) — reconcile the §11.1
  video-seconds ledger.

A resource is "wired" if it is a **callable** ``async () -> Mapping | None`` (or
``async () -> int``); the handler awaits it and surfaces its return as the run
detail. The cadences below are sensible defaults; an entrypoint can re-register
with different triggers if it wants.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.jobs.registry import JobRegistry, job
from app.jobs.triggers import cron, every
from app.jobs.types import JobContext, JobResult

#: The resource keys each maintenance job looks for (a value here => "wired").
DIGEST_FLUSHER = "digest_flusher"
SEARCH_INDEXER = "search_indexer"
RETENTION_GC = "retention_gc"
IMPORT_RECOVERY = "import_recovery"
BUDGET_RECONCILER = "budget_reconciler"


async def _run_resource(ctx: JobContext, key: str) -> JobResult:
    """Shared body: resolve ``key`` from resources, await it, or skip cleanly.

    The resource is expected to be an awaitable-returning callable. A plain
    (already-awaitable) coroutine function works; anything else that is not
    callable yields a clean skip rather than an error, keeping the registration
    no-op-safe in every environment.
    """
    target = ctx.resource(key)
    if target is None:
        return JobResult.skipped(f"{key} not wired")
    if not callable(target):
        return JobResult.skipped(f"{key} is not callable")
    outcome: Any = target()
    if isinstance(outcome, Awaitable):
        outcome = await outcome
    if isinstance(outcome, dict):
        return JobResult.ok(**outcome)
    if isinstance(outcome, int):
        return JobResult.ok(processed=outcome)
    return JobResult.ok(result=str(outcome) if outcome is not None else "done")


def register_maintenance_jobs(registry: JobRegistry) -> JobRegistry:
    """Register the five built-in maintenance jobs into ``registry``.

    Idempotent-ish only in that registering twice raises (duplicate name); call
    once per registry. Returns the registry for chaining.
    """

    @job(
        "maintenance.digest_flush",
        trigger=every(60),  # flush batched digests once a minute
        max_attempts=3,
        description="Flush batched notification/event digests (no-op if unwired).",
        registry=registry,
    )
    async def digest_flush(ctx: JobContext) -> JobResult:
        return await _run_resource(ctx, DIGEST_FLUSHER)

    @job(
        "maintenance.search_index_refresh",
        trigger=every(300),  # refresh the search index every 5 minutes
        max_attempts=3,
        description="Refresh/rebuild the search index (no-op if unwired).",
        registry=registry,
    )
    async def search_index_refresh(ctx: JobContext) -> JobResult:
        return await _run_resource(ctx, SEARCH_INDEXER)

    @job(
        "maintenance.retention_gc",
        trigger=cron("0 3 * * *"),  # nightly GC sweep at 03:00 UTC
        max_attempts=2,
        description="Sweep expired/orphaned artifacts (no-op if unwired).",
        registry=registry,
    )
    async def retention_gc(ctx: JobContext) -> JobResult:
        return await _run_resource(ctx, RETENTION_GC)

    @job(
        "maintenance.stuck_import_recovery",
        trigger=every(120),  # re-drive stuck imports on a 2-minute cadence (§4.7 sibling)
        max_attempts=2,
        description="Re-drive books stuck 'importing' (no-op if unwired).",
        registry=registry,
    )
    async def stuck_import_recovery(ctx: JobContext) -> JobResult:
        return await _run_resource(ctx, IMPORT_RECOVERY)

    @job(
        "maintenance.budget_reconcile",
        trigger=every(900),  # reconcile the budget ledger every 15 minutes (§11.1)
        max_attempts=3,
        description="Reconcile the video-seconds budget ledger (no-op if unwired).",
        registry=registry,
    )
    async def budget_reconcile(ctx: JobContext) -> JobResult:
        return await _run_resource(ctx, BUDGET_RECONCILER)

    return registry


def default_maintenance_registry() -> JobRegistry:
    """A fresh registry with only the built-in maintenance jobs registered."""
    return register_maintenance_jobs(JobRegistry())


#: The maintenance job names, for callers that want to pause/inspect them.
MAINTENANCE_JOB_NAMES: tuple[str, ...] = (
    "maintenance.digest_flush",
    "maintenance.search_index_refresh",
    "maintenance.retention_gc",
    "maintenance.stuck_import_recovery",
    "maintenance.budget_reconcile",
)

#: Convenience type alias for a wired maintenance resource.
MaintenanceResource = Callable[[], Awaitable[Any]]


__all__ = [
    "BUDGET_RECONCILER",
    "DIGEST_FLUSHER",
    "IMPORT_RECOVERY",
    "MAINTENANCE_JOB_NAMES",
    "RETENTION_GC",
    "SEARCH_INDEXER",
    "MaintenanceResource",
    "default_maintenance_registry",
    "register_maintenance_jobs",
]
