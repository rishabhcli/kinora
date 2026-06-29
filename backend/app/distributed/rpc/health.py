"""Health checking — what discovery and the breaker trust to route around faults.

Two flavours, both standard in a mesh:

* **Liveness / readiness checks** (:class:`HealthCheck`): a named async probe that
  returns a :class:`HealthStatus`. A service composes several (its DB pool, its
  Redis queue, its downstream deps) into one :class:`HealthChecker`; the
  aggregate is the *worst* component status (one ``UNHEALTHY`` dep makes the
  service unhealthy, any ``DEGRADED`` makes it degraded). A check that itself
  raises or exceeds its own timeout counts as ``UNHEALTHY`` — a hung probe is a
  failure, not an unknown.

* **Passive outlier detection** (:class:`OutlierDetector`): infer health from the
  *call stream* without a dedicated probe. Consecutive transport failures eject an
  instance from rotation for a cooldown (Envoy-style outlier ejection), and a
  success window reinstates it. This catches the "process is up but wedged" case
  the active probe misses between intervals.

Everything is clock-injected and infra-free: the checks a real service wires
(``async def`` over a DB ping) are *its* business; this module gives the framework
that runs, times, aggregates, and acts on them deterministically.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from app.distributed.rpc.deadline import Clock, SystemClock
from app.distributed.rpc.errors import FailureKind, RpcError
from app.distributed.rpc.registry import InstanceHealth


class HealthStatus(enum.Enum):
    """A health probe's verdict (orders worst→best for aggregation)."""

    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"
    HEALTHY = "healthy"

    @property
    def rank(self) -> int:
        """Numeric rank (lower = worse) so aggregation can take the minimum."""
        return {HealthStatus.UNHEALTHY: 0, HealthStatus.DEGRADED: 1, HealthStatus.HEALTHY: 2}[self]

    def to_instance_health(self) -> InstanceHealth:
        """Map onto the registry's :class:`InstanceHealth`."""
        return {
            HealthStatus.HEALTHY: InstanceHealth.HEALTHY,
            HealthStatus.DEGRADED: InstanceHealth.DEGRADED,
            HealthStatus.UNHEALTHY: InstanceHealth.UNHEALTHY,
        }[self]


#: A health probe: ``async () -> HealthStatus``. Raising counts as UNHEALTHY.
HealthProbe = Callable[[], Awaitable[HealthStatus]]


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """A named health probe with its own timeout + criticality.

    A non-critical check that fails downgrades the aggregate to ``DEGRADED`` (the
    service still serves, just not at full capability); a critical check that
    fails makes the aggregate ``UNHEALTHY``.
    """

    name: str
    probe: HealthProbe
    timeout_s: float = 1.0
    critical: bool = True


@dataclass(frozen=True, slots=True)
class ComponentResult:
    """One check's outcome within a :class:`HealthReport`."""

    name: str
    status: HealthStatus
    latency_s: float
    error: str | None = None


@dataclass(frozen=True, slots=True)
class HealthReport:
    """The aggregated health of a service across its component checks."""

    status: HealthStatus
    components: tuple[ComponentResult, ...] = ()

    @property
    def healthy(self) -> bool:
        """True when the aggregate is fully healthy."""
        return self.status is HealthStatus.HEALTHY

    def to_dict(self) -> dict[str, object]:
        """A JSON-ready view (for a ``/healthz`` endpoint / dashboard)."""
        return {
            "status": self.status.value,
            "components": [
                {
                    "name": c.name,
                    "status": c.status.value,
                    "latency_s": round(c.latency_s, 6),
                    "error": c.error,
                }
                for c in self.components
            ],
        }


@dataclass
class HealthChecker:
    """Runs a service's health checks and aggregates them into a report.

    Each check is timed against the injected clock and bounded by its own
    ``timeout_s`` (a probe that overshoots is recorded ``UNHEALTHY``). The
    aggregate is the worst critical status, downgraded one rung for any failing
    non-critical check — the standard liveness/readiness rollup.
    """

    checks: list[HealthCheck] = field(default_factory=list)
    clock: Clock = field(default_factory=SystemClock)

    def add(self, check: HealthCheck) -> None:
        """Register a component health check."""
        self.checks.append(check)

    async def run(self) -> HealthReport:
        """Run every check (with timeouts) and aggregate the verdict."""
        import anyio

        results: list[ComponentResult] = []
        for check in self.checks:
            start = self.clock.now()
            status = HealthStatus.HEALTHY
            error: str | None = None
            try:
                with anyio.fail_after(check.timeout_s):
                    status = await check.probe()
            except TimeoutError:
                status = HealthStatus.UNHEALTHY
                error = f"probe timed out after {check.timeout_s}s"
            except Exception as exc:  # a raising probe is a failure, not unknown
                status = HealthStatus.UNHEALTHY
                error = f"{type(exc).__name__}: {exc}"
            results.append(
                ComponentResult(
                    name=check.name,
                    status=status,
                    latency_s=max(0.0, self.clock.now() - start),
                    error=error,
                )
            )
        return HealthReport(status=self._aggregate(results), components=tuple(results))

    def _aggregate(self, results: Sequence[ComponentResult]) -> HealthStatus:
        if not results:
            return HealthStatus.HEALTHY
        worst = HealthStatus.HEALTHY
        for check, result in zip(self.checks, results, strict=True):
            if result.status is HealthStatus.HEALTHY:
                continue
            effective = result.status
            if not check.critical and result.status is HealthStatus.UNHEALTHY:
                # A non-critical failure degrades rather than fails the service.
                effective = HealthStatus.DEGRADED
            if effective.rank < worst.rank:
                worst = effective
        return worst


@dataclass(frozen=True, slots=True)
class OutlierConfig:
    """Tuning for passive outlier ejection."""

    consecutive_failures: int = 5
    ejection_base_s: float = 5.0
    max_ejection_s: float = 60.0
    success_reinstate: int = 2


@dataclass
class OutlierDetector:
    """Passive per-instance outlier ejection inferred from the call stream.

    Feed it every call outcome via :meth:`record`; ask :meth:`is_ejected` before
    routing to an instance. Consecutive transport failures eject the instance for
    a cooldown that grows with repeated ejections (capped), and a run of successes
    reinstates it — without any active probe traffic.
    """

    config: OutlierConfig = field(default_factory=OutlierConfig)
    clock: Clock = field(default_factory=SystemClock)
    _consecutive_failures: dict[str, int] = field(default_factory=dict)
    _consecutive_successes: dict[str, int] = field(default_factory=dict)
    _ejected_until: dict[str, float] = field(default_factory=dict)
    _ejection_count: dict[str, int] = field(default_factory=dict)

    def record(self, instance_id: str, error: RpcError | None) -> None:
        """Record one call outcome for an instance (``None`` error = success)."""
        is_transport_failure = error is not None and error.kind is FailureKind.TRANSPORT
        if is_transport_failure:
            self._consecutive_successes[instance_id] = 0
            n = self._consecutive_failures.get(instance_id, 0) + 1
            self._consecutive_failures[instance_id] = n
            if n >= self.config.consecutive_failures:
                self._eject(instance_id)
        else:
            self._consecutive_failures[instance_id] = 0
            s = self._consecutive_successes.get(instance_id, 0) + 1
            self._consecutive_successes[instance_id] = s
            if s >= self.config.success_reinstate:
                self._ejected_until.pop(instance_id, None)

    def _eject(self, instance_id: str) -> None:
        count = self._ejection_count.get(instance_id, 0) + 1
        self._ejection_count[instance_id] = count
        cooldown = min(
            self.config.max_ejection_s,
            self.config.ejection_base_s * (2 ** (count - 1)),
        )
        self._ejected_until[instance_id] = self.clock.now() + cooldown
        self._consecutive_failures[instance_id] = 0

    def is_ejected(self, instance_id: str) -> bool:
        """Whether an instance is currently ejected from rotation."""
        until = self._ejected_until.get(instance_id)
        if until is None:
            return False
        if self.clock.now() >= until:
            self._ejected_until.pop(instance_id, None)
            return False
        return True

    def ejected_instances(self) -> list[str]:
        """The currently-ejected instance ids (observability)."""
        return [iid for iid in self._ejected_until if self.is_ejected(iid)]


def always_healthy() -> HealthProbe:
    """A trivial probe that always reports HEALTHY (a default / placeholder)."""

    async def _probe() -> HealthStatus:
        return HealthStatus.HEALTHY

    return _probe


__all__ = [
    "ComponentResult",
    "HealthCheck",
    "HealthChecker",
    "HealthProbe",
    "HealthReport",
    "HealthStatus",
    "OutlierConfig",
    "OutlierDetector",
    "always_healthy",
]
