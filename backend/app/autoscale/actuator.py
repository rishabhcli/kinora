"""Orchestrator actuator seam — apply a :class:`ScalingPlan` (kinora.md §12.1).

The controller decides *desired* replicas; the actuator makes it so. Production
backends differ (a Kubernetes Deployment's ``spec.replicas``, an ECS service
desired-count, a process supervisor's worker TaskGroup), so this module defines
only the **interface** plus a no-infra recording implementation. Concrete adapters
(k8s/ECS/process) live behind the same protocol and are wired by the composition
root — none are implemented here because none can be exercised without infra, and
this subsystem must stay test-only-deterministic.

:class:`Actuator` is async (real orchestrators are network calls). :class:`RecordingActuator`
keeps an in-memory replica map and an applied-plan log, so the simulator and tests
drive the full controller -> actuator loop with zero I/O. :class:`KubernetesActuatorStub`
documents the contract a real adapter fills and raises ``NotImplementedError`` —
it is a typed placeholder, never a live scaler.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.autoscale.controller import ScalingPlan
from app.autoscale.lanes import Lane
from app.core.logging import get_logger

logger = get_logger("app.autoscale.actuator")

__all__ = [
    "Actuator",
    "AppliedScaling",
    "KubernetesActuatorStub",
    "RecordingActuator",
]


class AppliedScaling:
    """Immutable record of one applied plan (what the actuator actually did)."""

    __slots__ = ("desired", "previous")

    def __init__(self, desired: dict[Lane, int], previous: dict[Lane, int]) -> None:
        self.desired = dict(desired)
        self.previous = dict(previous)

    @property
    def deltas(self) -> dict[Lane, int]:
        return {lane: self.desired[lane] - self.previous.get(lane, 0) for lane in self.desired}

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"AppliedScaling(deltas={self.deltas})"


@runtime_checkable
class Actuator(Protocol):
    """Applies desired per-lane replica counts to an orchestrator.

    Implementations must be idempotent: applying the same plan twice is a no-op on
    the second call. They must never *raise to block the control loop* on a
    transient orchestrator error — log and let the next tick re-converge.
    """

    async def current_replicas(self) -> dict[Lane, int]:  # pragma: no cover - protocol
        """Read the live replica count per lane from the orchestrator."""
        ...

    async def apply(self, plan: ScalingPlan) -> AppliedScaling:  # pragma: no cover - protocol
        """Drive each lane toward ``plan.desired``; return what was applied."""
        ...


class RecordingActuator:
    """In-memory actuator: applies plans to a local replica map, logs each apply.

    The default actuator for the simulator and tests — no orchestrator, no network.
    Faithfully models idempotency and the applied-plan history a real adapter would
    expose for observability.
    """

    def __init__(self, initial: dict[Lane, int] | None = None) -> None:
        self._replicas: dict[Lane, int] = dict(initial or {})
        self.history: list[AppliedScaling] = []

    async def current_replicas(self) -> dict[Lane, int]:
        return dict(self._replicas)

    async def apply(self, plan: ScalingPlan) -> AppliedScaling:
        previous = dict(self._replicas)
        for lane, desired in plan.desired.items():
            self._replicas[lane] = desired
        applied = AppliedScaling(desired=plan.desired, previous=previous)
        self.history.append(applied)
        if any(applied.deltas.values()):
            logger.info(
                "autoscale.apply",
                deltas={lane.value: dv for lane, dv in applied.deltas.items() if dv},
            )
        return applied


class KubernetesActuatorStub:
    """Typed contract for a real Kubernetes/ECS adapter — **not** implemented.

    A real adapter would, per lane, patch the corresponding Deployment's
    ``spec.replicas`` (or ECS service desired-count) and read it back. It is left
    unimplemented on purpose: this subsystem ships deterministic + infra-free, and a
    live cluster client cannot be exercised in the test suite. Wire a concrete
    subclass in the composition root for production.
    """

    def __init__(self, *, deployment_by_lane: dict[Lane, str]) -> None:
        self.deployment_by_lane = dict(deployment_by_lane)

    async def current_replicas(self) -> dict[Lane, int]:  # pragma: no cover - stub
        raise NotImplementedError(
            "KubernetesActuatorStub is an interface placeholder; wire a real client "
            "(kubernetes_asyncio / boto3 ECS) in the composition root."
        )

    async def apply(self, plan: ScalingPlan) -> AppliedScaling:  # pragma: no cover - stub
        raise NotImplementedError(
            "KubernetesActuatorStub is an interface placeholder; wire a real client "
            "(kubernetes_asyncio / boto3 ECS) in the composition root."
        )
