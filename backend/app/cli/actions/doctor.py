"""The ``doctor`` action — a one-shot health probe across every dependency.

Surfaces the §12.5 observability picture an operator needs at a glance: are
Postgres, Redis, and object storage reachable; is the render queue answerable;
what is the budget gate / live-video gate doing; and a quick census of the core
tables. Each probe is guarded so a single unreachable dependency degrades to a
``fail`` row rather than aborting the whole report — the command still exits
non-zero so it is usable in a health-check script.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlalchemy import func, select, text

from app.cli.errors import EXIT_OK, EXIT_UNAVAILABLE
from app.cli.formatting import humanize_seconds
from app.cli.output import Payload, Table
from app.composition import Container
from app.db.models.book import Book
from app.db.models.budget import BudgetLedger
from app.db.models.render_job import RenderJob
from app.db.models.user import User


@dataclass(frozen=True, slots=True)
class Check:
    """A single dependency probe result."""

    name: str
    ok: bool
    detail: str
    latency_ms: float | None = None

    @property
    def status(self) -> str:
        return "ok" if self.ok else "FAIL"


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """The aggregate of every probe plus a derived overall health flag."""

    checks: tuple[Check, ...]
    counts: dict[str, int] = field(default_factory=dict)
    budget_remaining_s: float | None = None
    budget_ceiling_s: float | None = None
    live_video: bool = False

    @property
    def healthy(self) -> bool:
        """True only when every probe passed."""
        return all(c.ok for c in self.checks)

    @property
    def exit_code(self) -> int:
        return EXIT_OK if self.healthy else EXIT_UNAVAILABLE

    def render_payload(self) -> Payload:
        data = {
            "healthy": self.healthy,
            "live_video": self.live_video,
            "budget_remaining_s": self.budget_remaining_s,
            "budget_ceiling_s": self.budget_ceiling_s,
            "counts": self.counts,
            "checks": [
                {
                    "name": c.name,
                    "ok": c.ok,
                    "detail": c.detail,
                    "latency_ms": c.latency_ms,
                }
                for c in self.checks
            ],
        }
        checks_table = Table(
            title="dependency checks",
            columns=("check", "status", "latency", "detail"),
            rows=[
                (
                    c.name,
                    c.status,
                    f"{c.latency_ms:.0f}ms" if c.latency_ms is not None else "-",
                    c.detail,
                )
                for c in self.checks
            ],
        )
        summary = Table(
            title="summary",
            columns=("field", "value"),
            rows=[
                ("healthy", "yes" if self.healthy else "NO"),
                ("live_video", "on" if self.live_video else "off"),
                (
                    "budget_remaining",
                    (
                        humanize_seconds(self.budget_remaining_s)
                        if self.budget_remaining_s is not None
                        else "-"
                    ),
                ),
                (
                    "budget_ceiling",
                    (
                        humanize_seconds(self.budget_ceiling_s)
                        if self.budget_ceiling_s is not None
                        else "-"
                    ),
                ),
                *[(f"rows.{name}", str(count)) for name, count in self.counts.items()],
            ],
        )
        return Payload.of(data, summary, checks_table)


async def _timed_postgres(container: Container) -> tuple[Check, dict[str, int]]:
    start = time.perf_counter()
    counts: dict[str, int] = {}
    try:
        async with container.session_factory() as db:
            await db.execute(text("SELECT 1"))
            for label, model in (
                ("books", Book),
                ("users", User),
                ("render_jobs", RenderJob),
                ("budget_ledger", BudgetLedger),
            ):
                value = (await db.execute(select(func.count()).select_from(model))).scalar_one()
                counts[label] = int(value)
        latency = (time.perf_counter() - start) * 1000
        return Check("postgres", True, "SELECT 1 ok", latency), counts
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        latency = (time.perf_counter() - start) * 1000
        return Check("postgres", False, str(exc)[:200], latency), counts


async def _timed_redis(container: Container) -> Check:
    start = time.perf_counter()
    try:
        pong = await container.redis.ping()
        latency = (time.perf_counter() - start) * 1000
        return Check("redis", bool(pong), "PING ok" if pong else "PING returned false", latency)
    except Exception as exc:  # noqa: BLE001
        latency = (time.perf_counter() - start) * 1000
        return Check("redis", False, str(exc)[:200], latency)


async def _timed_queue(container: Container) -> Check:
    start = time.perf_counter()
    try:
        stats = await container.queue.stats()
        latency = (time.perf_counter() - start) * 1000
        return Check(
            "render_queue",
            True,
            f"queued={stats.total_queued} processing={stats.processing} dlq={stats.dlq}",
            latency,
        )
    except Exception as exc:  # noqa: BLE001
        latency = (time.perf_counter() - start) * 1000
        return Check("render_queue", False, str(exc)[:200], latency)


def _object_store_check(container: Container) -> Check:
    start = time.perf_counter()
    try:
        # Construct the client + resolve the bucket without a network round-trip;
        # a misconfigured store fails here loudly rather than later mid-render.
        store = container.object_store
        bucket = getattr(store, "bucket", None) or getattr(store, "_bucket", "?")
        latency = (time.perf_counter() - start) * 1000
        return Check("object_store", True, f"bucket={bucket}", latency)
    except Exception as exc:  # noqa: BLE001
        latency = (time.perf_counter() - start) * 1000
        return Check("object_store", False, str(exc)[:200], latency)


async def run_doctor(container: Container) -> DoctorReport:
    """Probe every dependency and assemble a :class:`DoctorReport` (never raises)."""
    pg_check, counts = await _timed_postgres(container)
    redis_check = await _timed_redis(container)
    queue_check = await _timed_queue(container)
    store_check = _object_store_check(container)

    budget_remaining: float | None = None
    limits = container.budget_limits
    if pg_check.ok:
        try:
            from app.db.repositories.budget import BudgetRepo
            from app.memory.budget_service import BudgetService

            async with container.session_factory() as db:
                service = BudgetService(repo=BudgetRepo(db), limits=limits)
                budget_remaining = await service.remaining()
        except Exception:  # noqa: BLE001 - best-effort enrichment
            budget_remaining = None

    return DoctorReport(
        checks=(pg_check, redis_check, queue_check, store_check),
        counts=counts,
        budget_remaining_s=budget_remaining,
        budget_ceiling_s=limits.ceiling_video_s,
        live_video=limits.live_video,
    )


__all__ = ["Check", "DoctorReport", "run_doctor"]
