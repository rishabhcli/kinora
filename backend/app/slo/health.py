"""Deep dependency health-check framework (kinora.md §12).

The round-1 ``/ready`` probe answers a flat boolean per dependency (postgres,
redis). That is enough for a k8s readiness gate but blind to two production
realities Kinora cares about:

* **Criticality.** A degraded *non-critical* dependency (object store slow, the
  provider quota throttling) should not take the instance out of rotation — the
  film still plays from the degradation ladder. Only a *critical* dependency
  down (postgres, redis) means "stop routing traffic". So this framework
  distinguishes ``critical`` from ``optional`` probes and folds a non-critical
  failure into **degraded** (still ready) rather than **down** (not ready).

* **Timeouts.** A dependency that hangs is worse than one that errors — it ties
  up the probe. Every probe runs under a per-probe timeout; a timeout is a
  distinct :class:`HealthStatus.TIMEOUT` outcome, never an unbounded await.

Probes are injectable async callables (DB ``SELECT 1``, Redis ``PING``, object
store HEAD, provider preflight, MCP ping) so the framework has **zero** infra
imports and tests drive it with synthetic in-memory probes. Probes are evaluated
**in parallel** (``asyncio.gather``) so the aggregate latency is the slowest
probe, not their sum.

Liveness vs readiness: :meth:`HealthRegistry.liveness` answers "is the process
itself alive" (it never touches a dependency — a liveness failure means *restart
me*); :meth:`HealthRegistry.readiness` runs the probes and answers "can I serve
traffic right now" (a readiness failure means *route around me*).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

import structlog

logger = structlog.get_logger(__name__)


class HealthStatus(StrEnum):
    """The outcome of a single probe (ordered worst-last by :data:`_RANK`)."""

    UP = "up"  # probe returned healthy
    DEGRADED = "degraded"  # probe returned but reported impaired (slow / partial)
    TIMEOUT = "timeout"  # probe exceeded its deadline
    DOWN = "down"  # probe raised or returned unhealthy


#: Severity ordering so an aggregate can pick the *worst* observed status.
_RANK = {
    HealthStatus.UP: 0,
    HealthStatus.DEGRADED: 1,
    HealthStatus.TIMEOUT: 2,
    HealthStatus.DOWN: 3,
}


class Criticality(StrEnum):
    """Whether a dependency failing takes the instance out of rotation."""

    CRITICAL = "critical"  # down/timeout => the instance is NOT ready
    OPTIONAL = "optional"  # down/timeout => the instance is DEGRADED but ready


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The structured result a probe returns (or the framework synthesises)."""

    status: HealthStatus
    detail: str = ""
    #: Free-form facts a probe wants surfaced (version, queue depth, lag…).
    data: dict[str, object] = field(default_factory=dict)

    @classmethod
    def up(cls, detail: str = "", **data: object) -> ProbeResult:
        return cls(HealthStatus.UP, detail, dict(data))

    @classmethod
    def degraded(cls, detail: str = "", **data: object) -> ProbeResult:
        return cls(HealthStatus.DEGRADED, detail, dict(data))

    @classmethod
    def down(cls, detail: str = "", **data: object) -> ProbeResult:
        return cls(HealthStatus.DOWN, detail, dict(data))


#: A probe is any async callable returning a :class:`ProbeResult`. Returning a
#: bare ``bool`` is also accepted (True=>UP, False=>DOWN) for terse probes.
ProbeFn = Callable[[], Awaitable["ProbeResult | bool"]]


@dataclass(frozen=True, slots=True)
class HealthProbe:
    """A named dependency probe with a timeout and a criticality."""

    name: str
    probe: ProbeFn
    criticality: Criticality = Criticality.CRITICAL
    timeout_s: float = 2.0

    async def evaluate(self) -> ProbeOutcome:
        """Run the probe under its timeout; never raises, always returns an outcome."""
        started = time.monotonic()
        try:
            raw = await asyncio.wait_for(self.probe(), timeout=self.timeout_s)
        except TimeoutError:
            elapsed = (time.monotonic() - started) * 1000.0
            logger.warning("slo.health.timeout", probe=self.name, timeout_s=self.timeout_s)
            return ProbeOutcome(
                probe=self,
                result=ProbeResult(HealthStatus.TIMEOUT, f"timed out after {self.timeout_s}s"),
                latency_ms=elapsed,
            )
        except Exception as exc:  # noqa: BLE001 - a probe must never crash the health plane
            elapsed = (time.monotonic() - started) * 1000.0
            logger.warning("slo.health.probe_error", probe=self.name, error=str(exc))
            return ProbeOutcome(
                probe=self,
                result=ProbeResult(HealthStatus.DOWN, f"{type(exc).__name__}: {exc}"),
                latency_ms=elapsed,
            )
        elapsed = (time.monotonic() - started) * 1000.0
        if isinstance(raw, bool):
            result = ProbeResult.up() if raw else ProbeResult.down("probe returned false")
        else:
            result = raw
        return ProbeOutcome(probe=self, result=result, latency_ms=elapsed)


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """A probe's :class:`HealthProbe` + its measured :class:`ProbeResult`."""

    probe: HealthProbe
    result: ProbeResult
    latency_ms: float

    @property
    def name(self) -> str:
        return self.probe.name

    @property
    def status(self) -> HealthStatus:
        return self.result.status

    @property
    def healthy(self) -> bool:
        """True when the probe is fully up (DEGRADED is *not* healthy here)."""
        return self.status is HealthStatus.UP

    @property
    def is_blocking(self) -> bool:
        """True when this outcome should fail readiness.

        A *critical* probe that is not UP blocks (DEGRADED critical also blocks —
        a critical dependency reporting impairment is a readiness concern). An
        *optional* probe never blocks; its failure only degrades the aggregate.
        """
        if self.probe.criticality is Criticality.OPTIONAL:
            return False
        return self.status is not HealthStatus.UP

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "name": self.name,
            "status": self.status.value,
            "criticality": self.probe.criticality.value,
            "latency_ms": round(self.latency_ms, 2),
        }
        if self.result.detail:
            out["detail"] = self.result.detail
        if self.result.data:
            out["data"] = dict(self.result.data)
        return out


@dataclass(frozen=True, slots=True)
class HealthReport:
    """The aggregated result of running every probe once."""

    status: HealthStatus
    ready: bool
    outcomes: tuple[ProbeOutcome, ...]
    duration_ms: float

    @property
    def degraded(self) -> bool:
        return self.status is HealthStatus.DEGRADED

    @property
    def blocking(self) -> tuple[ProbeOutcome, ...]:
        """The critical-and-not-UP outcomes that drove readiness to false."""
        return tuple(o for o in self.outcomes if o.is_blocking)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "ready": self.ready,
            "duration_ms": round(self.duration_ms, 2),
            "dependencies": [o.to_dict() for o in self.outcomes],
        }


def aggregate(outcomes: tuple[ProbeOutcome, ...]) -> tuple[HealthStatus, bool]:
    """Fold probe outcomes into an (aggregate status, ready) verdict.

    The aggregate **status** is the worst observed status across all probes —
    so a single optional dependency timing out surfaces as ``degraded`` in the
    report even though the instance stays ready. **Ready** is driven only by the
    *blocking* (critical, not-UP) outcomes: any blocking outcome => not ready.

    A critical dependency being merely DEGRADED yields aggregate ``degraded`` AND
    ``ready=False`` (the instance reports a problem and is pulled), whereas an
    optional dependency being DOWN yields aggregate ``down`` status but
    ``ready=True`` — the worst *status* still tells operators something is wrong
    without taking traffic.
    """
    if not outcomes:
        return HealthStatus.UP, True
    worst = max((o.status for o in outcomes), key=lambda s: _RANK[s])
    ready = not any(o.is_blocking for o in outcomes)
    return worst, ready


@dataclass(slots=True)
class HealthRegistry:
    """A mutable registry of probes feeding the readiness endpoint.

    ``live`` is the liveness flag: the process can mark itself draining /
    unhealthy (e.g. a fatal background-task crash) so liveness fails and the
    orchestrator restarts it, independently of any dependency. Default True.
    """

    probes: list[HealthProbe] = field(default_factory=list)
    live: bool = True

    def register(
        self,
        name: str,
        probe: ProbeFn,
        *,
        criticality: Criticality = Criticality.CRITICAL,
        timeout_s: float = 2.0,
    ) -> HealthProbe:
        """Add a probe (replacing any existing probe of the same name)."""
        self.probes = [p for p in self.probes if p.name != name]
        hp = HealthProbe(name=name, probe=probe, criticality=criticality, timeout_s=timeout_s)
        self.probes.append(hp)
        return hp

    def liveness(self) -> dict[str, object]:
        """Pure liveness — never touches a dependency; just the process flag."""
        return {"status": "alive" if self.live else "draining", "live": self.live}

    async def readiness(self) -> HealthReport:
        """Run every probe in parallel under its timeout and aggregate."""
        started = time.monotonic()
        if not self.probes:
            return HealthReport(HealthStatus.UP, True, (), 0.0)
        outcomes = tuple(
            await asyncio.gather(*(p.evaluate() for p in self.probes), return_exceptions=False)
        )
        status, ready = aggregate(outcomes)
        # Liveness gates readiness: a draining process is never ready.
        ready = ready and self.live
        if not self.live:
            status = HealthStatus.DOWN
        duration = (time.monotonic() - started) * 1000.0
        return HealthReport(status=status, ready=ready, outcomes=outcomes, duration_ms=duration)


__all__ = [
    "Criticality",
    "HealthProbe",
    "HealthRegistry",
    "HealthReport",
    "HealthStatus",
    "ProbeFn",
    "ProbeOutcome",
    "ProbeResult",
    "aggregate",
]
