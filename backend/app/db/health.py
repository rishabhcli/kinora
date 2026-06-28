"""Connection-pool health checks and pool-stats snapshots.

Two concerns the readiness gate (§12, ``/ready``) and the observability panel
(§12.5) both need:

* **liveness** — can we open a connection and run ``SELECT 1`` right now? This is
  what :meth:`Container._check_postgres` already does inline; :func:`ping`
  generalises it to any engine (primary *or* replica) and never raises.
* **pool stats** — how saturated is the connection pool? A pool pinned at
  ``size + overflow`` with a growing checkout count is the database-side signal
  of the same backpressure the render queue reasons about (§12.2).

Nothing here raises: a health probe that crashes is worse than one that reports
``False``, so every failure is caught, logged, and returned as a negative result.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import QueuePool

from app.core.logging import get_logger
from app.db.engine import EngineRegistry, get_recorder

logger = get_logger("app.db.health")


@dataclass(slots=True)
class PoolStats:
    """A point-in-time view of one engine's connection pool.

    Fields mirror SQLAlchemy's :class:`~sqlalchemy.pool.QueuePool` introspection.
    ``checked_out`` rising toward ``size + overflow`` while ``overflow`` is
    positive is the saturation signal worth alerting on.
    """

    pool_class: str
    size: int | None
    checked_in: int | None
    checked_out: int | None
    overflow: int | None
    total_capacity: int | None

    @property
    def utilization(self) -> float | None:
        """Fraction of total capacity currently checked out (``None`` if unknown)."""
        if self.checked_out is None or not self.total_capacity:
            return None
        return round(self.checked_out / self.total_capacity, 4)

    @property
    def is_saturated(self) -> bool:
        """True when every slot (base + overflow) is checked out."""
        if self.checked_out is None or self.total_capacity is None:
            return False
        return self.checked_out >= self.total_capacity

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable view for the metrics surface."""
        return {
            "pool_class": self.pool_class,
            "size": self.size,
            "checked_in": self.checked_in,
            "checked_out": self.checked_out,
            "overflow": self.overflow,
            "total_capacity": self.total_capacity,
            "utilization": self.utilization,
            "is_saturated": self.is_saturated,
        }


def pool_stats(engine: AsyncEngine) -> PoolStats:
    """Snapshot the connection-pool counters for ``engine``.

    Works against a real :class:`QueuePool`; for a :class:`NullPool` (tests /
    one-shot scripts) the per-slot counters are ``None`` because there is no pool
    to introspect.
    """
    pool = engine.pool
    pool_class = type(pool).__name__
    if isinstance(pool, QueuePool):
        size = pool.size()
        overflow = pool.overflow()
        checked_out = pool.checkedout()
        checked_in = pool.checkedin()
        # ``size`` is the base pool size; ``max_overflow`` is private but the
        # total capacity is base + the configured overflow ceiling. ``overflow``
        # here is the *current* overflow count, which can be negative when the
        # base pool is not yet full; clamp for the capacity calculation.
        max_overflow = getattr(pool, "_max_overflow", 0)
        total_capacity = size + max(0, max_overflow)
        return PoolStats(
            pool_class=pool_class,
            size=size,
            checked_in=checked_in,
            checked_out=checked_out,
            overflow=overflow,
            total_capacity=total_capacity,
        )
    return PoolStats(
        pool_class=pool_class,
        size=None,
        checked_in=None,
        checked_out=None,
        overflow=None,
        total_capacity=None,
    )


async def ping(engine: AsyncEngine, *, timeout_s: float | None = None) -> bool:
    """Open a connection and run ``SELECT 1``; return ``True`` iff it succeeds.

    Never raises — a readiness probe that throws turns a 503 into a 500. An
    optional ``timeout_s`` bounds how long the probe waits before reporting down.
    """
    import asyncio

    async def _probe() -> bool:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True

    try:
        if timeout_s is not None:
            return await asyncio.wait_for(_probe(), timeout=timeout_s)
        return await _probe()
    except Exception as exc:  # noqa: BLE001 - health probe must never raise
        logger.warning("db.health.ping_failed", error=str(exc))
        return False


@dataclass(slots=True)
class EngineHealth:
    """Liveness + latency + pool stats for one engine role."""

    role: str
    alive: bool
    latency_ms: float | None
    pool: PoolStats

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable health record."""
        return {
            "role": self.role,
            "alive": self.alive,
            "latency_ms": self.latency_ms,
            "pool": self.pool.as_dict(),
        }


async def engine_health(engine: AsyncEngine, *, role: str, timeout_s: float = 2.0) -> EngineHealth:
    """Probe one engine: ping latency + pool snapshot (never raises)."""
    start = time.perf_counter()
    alive = await ping(engine, timeout_s=timeout_s)
    latency_ms = round((time.perf_counter() - start) * 1000.0, 3) if alive else None
    return EngineHealth(role=role, alive=alive, latency_ms=latency_ms, pool=pool_stats(engine))


async def registry_health(
    registry: EngineRegistry, *, timeout_s: float = 2.0, ensure_built: bool = True
) -> dict[str, Any]:
    """Aggregate health across every engine in a registry (for ``/ready`` + metrics).

    With ``ensure_built`` the primary (and the replica when configured) are
    constructed so the probe reflects the live deployment even before first use;
    otherwise only already-built engines are probed.
    """
    if ensure_built:
        registry.writer()
        if registry.has_replica:
            registry.reader()

    roles: list[EngineHealth] = []
    for role, engine in registry.engines():
        roles.append(await engine_health(engine, role=role, timeout_s=timeout_s))

    overall = all(h.alive for h in roles) if roles else False
    result: dict[str, Any] = {
        "ok": overall,
        "engines": [h.as_dict() for h in roles],
    }
    # Fold in the slow-query counters when the primary is instrumented.
    recorder = get_recorder(registry.writer()) if registry.writer_built else None
    if recorder is not None:
        result["slow_queries"] = recorder.stats()
    return result


__all__ = [
    "EngineHealth",
    "PoolStats",
    "engine_health",
    "ping",
    "pool_stats",
    "registry_health",
]
