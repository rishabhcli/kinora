"""Health & readiness probing with stability windows (kinora.md §12.5).

The backend already exposes ``/health`` (liveness) and ``/ready`` (readiness —
actively SELECT 1 + Redis PING; 503 when a dependency is down), per
``backend/app/main.py``. This module is the *client* side of that contract used
during a rollout: it probes a target (a blue/green slot or canary fleet),
applies a **stability window** (N consecutive healthy samples before a target is
declared "ready"), and surfaces a clean :class:`HealthGate` the orchestrator
gates rollout steps on.

The probe itself is a :class:`HealthProbe` Protocol. Production wires an HTTP
probe that hits ``GET /ready`` on each instance; tests inject a scripted fake.
No HTTP client is imported here, so the gate logic is unit-testable offline.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from deploy.orchestrator.models import HealthStatus


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One health probe sample for a target."""

    status: HealthStatus
    #: Per-dependency checks, mirroring ``/ready``'s ``checks`` map
    #: (e.g. ``{"postgres": True, "redis": True}``).
    checks: dict[str, bool] = field(default_factory=dict)
    latency_ms: float = 0.0
    detail: str = ""

    @property
    def healthy(self) -> bool:
        return self.status is HealthStatus.HEALTHY

    @classmethod
    def ok(cls, **checks: bool) -> ProbeResult:
        return cls(status=HealthStatus.HEALTHY, checks=dict(checks) or {"ready": True})

    @classmethod
    def down(cls, detail: str = "", **checks: bool) -> ProbeResult:
        return cls(status=HealthStatus.UNHEALTHY, checks=dict(checks), detail=detail)


@runtime_checkable
class HealthProbe(Protocol):
    """Probes one logical target (a slot or a fleet of instances).

    Implementations aggregate per-instance ``/ready`` calls into a single
    :class:`ProbeResult` (e.g. healthy iff a quorum of instances are ready).
    Must be free of hidden time/network in tests — production wiring owns I/O.
    """

    async def probe(self, target: str) -> ProbeResult:
        """Return the current aggregated health of ``target``."""
        ...


@dataclass(slots=True)
class StabilityWindow:
    """Sliding window requiring N consecutive healthy samples to be "stable".

    A single healthy sample is not enough to trust a freshly started instance:
    it may flap (a worker that started, OOMed, restarted). The window declares
    stability only after ``required`` consecutive healthy probes, and resets the
    streak on any unhealthy sample. It also caps total samples (``max_samples``)
    so a flapping target eventually fails the gate instead of probing forever.
    """

    required: int = 3
    max_samples: int = 30
    _streak: int = field(default=0, init=False)
    _samples: int = field(default=0, init=False)
    _history: deque[HealthStatus] = field(default_factory=deque, init=False)

    def __post_init__(self) -> None:
        if self.required < 1:
            raise ValueError("required must be >= 1")
        if self.max_samples < self.required:
            raise ValueError("max_samples must be >= required")

    def observe(self, result: ProbeResult) -> None:
        self._samples += 1
        self._history.append(result.status)
        if len(self._history) > self.max_samples:
            self._history.popleft()
        if result.healthy:
            self._streak += 1
        else:
            self._streak = 0

    @property
    def streak(self) -> int:
        return self._streak

    @property
    def samples(self) -> int:
        return self._samples

    @property
    def is_stable(self) -> bool:
        return self._streak >= self.required

    @property
    def is_exhausted(self) -> bool:
        """True once the budget of samples is spent without reaching stability."""
        return self._samples >= self.max_samples and not self.is_stable

    def reset(self) -> None:
        self._streak = 0
        self._samples = 0
        self._history.clear()


class HealthGate:
    """Drives a :class:`HealthProbe` through a :class:`StabilityWindow`.

    ``await wait_until_stable(target)`` polls the probe until either the window
    declares stability (→ ``True``) or the sample budget is exhausted
    (→ ``False``). It does **not** sleep — the caller's scheduler/simulator owns
    pacing — so one ``observe`` happens per call to keep stepping deterministic.
    """

    __slots__ = ("_probe", "_window")

    def __init__(self, probe: HealthProbe, *, window: StabilityWindow | None = None) -> None:
        self._probe = probe
        self._window = window if window is not None else StabilityWindow()

    @property
    def window(self) -> StabilityWindow:
        return self._window

    async def sample(self, target: str) -> ProbeResult:
        """Take one probe sample and fold it into the window."""
        result = await self._probe.probe(target)
        self._window.observe(result)
        return result

    async def wait_until_stable(self, target: str) -> bool:
        """Poll until stable or exhausted. Returns whether the target is stable.

        Each iteration is a single probe; the loop terminates because the window
        is bounded by ``max_samples``.
        """
        self._window.reset()
        while True:
            await self.sample(target)
            if self._window.is_stable:
                return True
            if self._window.is_exhausted:
                return False


def quorum_status(results: Sequence[ProbeResult], *, min_healthy: float = 1.0) -> HealthStatus:
    """Aggregate per-instance probe results into one fleet status.

    ``min_healthy`` is the fraction of instances that must be healthy. The
    default ``1.0`` means *all* instances must be ready; ``0.5`` is a quorum.
    Used by HTTP probe adapters to collapse a fleet into one :class:`ProbeResult`.
    """
    if not results:
        return HealthStatus.UNKNOWN
    healthy = sum(1 for r in results if r.healthy)
    return (
        HealthStatus.HEALTHY
        if healthy / len(results) >= min_healthy
        else HealthStatus.UNHEALTHY
    )
