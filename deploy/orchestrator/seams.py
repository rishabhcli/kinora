"""Effectful seams the orchestrator drives (kinora.md §12.6).

These Protocols are the only places the orchestrator touches the outside world:
provisioning instances, shifting traffic, and reading metrics. Production wires
Alibaba adapters (ESS scaling groups / SLB weighted listeners / a CloudMonitor
or ``/metrics`` scraper); tests and the simulator wire in-memory fakes. Keeping
them tiny is deliberate — the rollout/rollback *decision* logic lives in
:mod:`~deploy.orchestrator.orchestrator` and is fully testable against fakes.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from deploy.orchestrator.models import Artifact, Environment, ServiceRole


@runtime_checkable
class Provisioner(Protocol):
    """Brings instances of an artifact up/down in an environment.

    For blue-green this stands up the *green* fleet; for canary it scales the
    new-version fleet to match the canary weight. ``teardown`` retires a fleet
    after a successful cut-over or a rollback.
    """

    async def provision(
        self, artifact: Artifact, env: Environment, role: ServiceRole, *, replicas: int
    ) -> str:
        """Bring up ``replicas`` instances; return an opaque fleet/slot id."""
        ...

    async def teardown(self, slot_id: str) -> None:
        """Retire a fleet/slot by id."""
        ...


@runtime_checkable
class TrafficRouter(Protocol):
    """Shifts a fraction of traffic to the new version.

    ``shift`` is the single primitive the rollout strategies are expressed in:
    set the new version's traffic weight to ``weight`` (0.0–1.0). Blue-green
    calls it twice (0.0 then 1.0); canary calls it per step. ``current_weight``
    lets the orchestrator assert the router actually moved.
    """

    async def shift(self, new_slot: str, weight: float) -> None:
        ...

    async def current_weight(self, new_slot: str) -> float:
        ...
