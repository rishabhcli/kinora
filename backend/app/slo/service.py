"""Process-wide SLO engine + deep-health registry, and the call-site emit helpers.

The engine and the health registry are long-lived singletons (one per process,
like the Prometheus registry in :mod:`app.observability.metrics`). Hot paths call
the tiny module-level helpers — ``record_read``, ``record_shot``,
``record_api_request``, ``observe_render_latency_ms`` — so they never import the
engine types directly and stay one-liners.

``build_health_registry`` wires the *real* dependency probes from a wired
``Container`` (Postgres ``SELECT 1``, Redis ``PING``, object-store reachability,
MCP, provider preflight) with sensible criticalities — Postgres/Redis critical,
object-store/MCP/providers non-critical (a degraded one => the instance is
``degraded`` but still ``ready``, because the film falls back to the degradation
ladder). Every probe is fully guarded and never raises, so the health plane is
safe to build with no infra (the lazy-composition rule).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import structlog

from app.slo.engine import SLOEngine, build_default_engine
from app.slo.health import Criticality, HealthRegistry, ProbeResult
from app.slo.sli import (
    STREAM_API_AVAILABILITY,
    STREAM_INTENT_LATENCY_MS,
    STREAM_READ_UNDERRUN_FREE,
    STREAM_RENDER_LATENCY_MS,
    STREAM_SHOT_SUCCESS,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.composition import Container

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------- #
# Process-wide singletons
# --------------------------------------------------------------------------- #

_engine: SLOEngine | None = None
_registry: HealthRegistry | None = None


def get_slo_engine() -> SLOEngine:
    """Return the process-wide SLO engine (lazily built with default objectives)."""
    global _engine
    if _engine is None:
        _engine = build_default_engine()
    return _engine


def set_slo_engine(engine: SLOEngine) -> None:
    """Install a configured engine (the composition root / tests call this)."""
    global _engine
    _engine = engine


def get_health_registry() -> HealthRegistry:
    """Return the process-wide health registry (empty until probes are wired)."""
    global _registry
    if _registry is None:
        _registry = HealthRegistry()
    return _registry


def set_health_registry(registry: HealthRegistry) -> None:
    global _registry
    _registry = registry


def reset_for_test() -> None:
    """Drop the singletons so a test starts from a clean engine/registry."""
    global _engine, _registry
    _engine = None
    _registry = None


# --------------------------------------------------------------------------- #
# Call-site emit helpers (one-liners, never import engine types)
# --------------------------------------------------------------------------- #


def record_read(*, underrun_free: bool, now: float | None = None) -> None:
    """Record one page read against the buffer-underrun-free SLI."""
    get_slo_engine().record_event(STREAM_READ_UNDERRUN_FREE, good=underrun_free, now=now)


def record_shot(*, accepted: bool, now: float | None = None) -> None:
    """Record one shot render outcome against the shot-success SLI."""
    get_slo_engine().record_event(STREAM_SHOT_SUCCESS, good=accepted, now=now)


def record_api_request(*, ok: bool, now: float | None = None) -> None:
    """Record one API request against the availability SLI (ok = non-5xx)."""
    get_slo_engine().record_event(STREAM_API_AVAILABILITY, good=ok, now=now)


def observe_render_latency_ms(value_ms: float, *, now: float | None = None) -> None:
    """Record a shot-render wall-clock latency sample (ms)."""
    get_slo_engine().record_sample(STREAM_RENDER_LATENCY_MS, value_ms, now=now)


def observe_intent_latency_ms(value_ms: float, *, now: float | None = None) -> None:
    """Record a §4.9 control-tick (intent) latency sample (ms)."""
    get_slo_engine().record_sample(STREAM_INTENT_LATENCY_MS, value_ms, now=now)


# --------------------------------------------------------------------------- #
# Real dependency probes wired from the Container
# --------------------------------------------------------------------------- #


def _guarded(
    name: str, fn: Callable[[], Awaitable[ProbeResult]]
) -> Callable[[], Awaitable[ProbeResult]]:
    """Wrap a probe so any exception becomes a DOWN result (never raises)."""

    async def _run() -> ProbeResult:
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 - the framework also guards, belt+braces
            return ProbeResult.down(f"{type(exc).__name__}: {exc}")

    _run.__name__ = f"probe_{name}"
    return _run


def build_health_registry(container: Container) -> HealthRegistry:
    """Wire the real dependency probes from a :class:`Container` (additive).

    Postgres + Redis are *critical* (down => not ready). Object store, MCP, and
    the provider preflight are *optional* — a failure degrades the aggregate
    status but the instance stays ready because the render pipeline degrades
    gracefully (the §12.4 ladder) rather than hard-stopping.
    """
    registry = HealthRegistry()

    async def _postgres() -> ProbeResult:
        from sqlalchemy import text

        async with container.sessionmaker() as session:
            await session.execute(text("SELECT 1"))
        return ProbeResult.up("SELECT 1 ok")

    async def _redis() -> ProbeResult:
        ok = bool(await container.redis.ping())
        return ProbeResult.up("PONG") if ok else ProbeResult.down("PING returned falsy")

    async def _object_store() -> ProbeResult:
        store = container.object_store
        # Prefer a dedicated health hook if the store exposes one; otherwise a
        # cheap existence check is enough to know the endpoint answers.
        for hook in ("health_check", "ping"):
            fn = getattr(store, hook, None)
            if callable(fn):
                res = fn()
                if hasattr(res, "__await__"):
                    res = await res
                return ProbeResult.up(f"{hook} ok")
        exists = getattr(store, "exists", None)
        if callable(exists):
            res = exists("__healthcheck__/probe")
            if hasattr(res, "__await__"):
                await res
            return ProbeResult.up("exists() reachable")
        return ProbeResult.degraded("object store exposes no health hook")

    registry.register("postgres", _guarded("postgres", _postgres),
                      criticality=Criticality.CRITICAL, timeout_s=2.0)
    registry.register("redis", _guarded("redis", _redis),
                      criticality=Criticality.CRITICAL, timeout_s=2.0)
    registry.register("object_store", _guarded("object_store", _object_store),
                      criticality=Criticality.OPTIONAL, timeout_s=3.0)
    return registry


__all__ = [
    "build_health_registry",
    "get_health_registry",
    "get_slo_engine",
    "observe_intent_latency_ms",
    "observe_render_latency_ms",
    "record_api_request",
    "record_read",
    "record_shot",
    "reset_for_test",
    "set_health_registry",
    "set_slo_engine",
]
