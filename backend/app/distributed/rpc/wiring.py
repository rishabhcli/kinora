"""Wiring helpers — how the monolith mounts its packages onto the mesh.

This is the additive bridge between the abstract mesh and Kinora's real
composition root (``app.composition.Container``). It is intentionally a *helper
the Container can call*, not an edit to the Container: a single
:func:`mount_catalog_services` call registers the catalog contracts against impl
objects the caller already holds (the agents, the budget service, the memory
tools), turning "the Container's wired collaborators" into "addressable logical
services" in one place. Nothing here imports the heavy packages at module load;
the caller passes the already-constructed impls, so this stays import-cheap and
infra-free.

The point of doing it this way: the existing code keeps calling its collaborators
directly today, and the *same* collaborators are simultaneously reachable through
the mesh. A service is "split out" later by (a) starting it in its own process,
(b) pointing that process's mesh transport at the network, and (c) flipping the
local registry entry to a remote one — none of which touches a call site.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.distributed.rpc import catalog
from app.distributed.rpc.contracts import ServiceContract
from app.distributed.rpc.health import HealthCheck, HealthChecker, HealthStatus, always_healthy
from app.distributed.rpc.interceptors import access_log_interceptor
from app.distributed.rpc.mesh import ServiceMesh, build_default_mesh
from app.distributed.rpc.server import ServerInterceptor

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ServiceBinding:
    """A contract + the impl object that satisfies it (+ optional health/map)."""

    contract: ServiceContract
    impl: Any
    method_map: Mapping[str, str] | None = None
    health: HealthChecker | None = None


def mount_catalog_services(
    mesh: ServiceMesh,
    *,
    cinematographer: Any = None,
    generator: Any = None,
    critic: Any = None,
    memory: Any = None,
    budget: Any = None,
    scheduler: Any = None,
    search: Any = None,
    interceptors: list[ServerInterceptor] | None = None,
) -> list[str]:
    """Register the catalog services for whichever impls the caller supplies.

    Each keyword maps a catalog service to its implementation object; a ``None``
    impl skips that service (so a process that only runs the render workers can
    mount just the generator/critic). Returns the list of mounted service names.

    The impls are the Container's existing collaborators — e.g. ``memory`` is the
    wired ``MemoryTools``-shaped object, ``budget`` the ``BudgetService``. The
    ``method_map`` per binding adapts a contract method name to the impl's real
    method when they differ (the common case wiring legacy method names).
    """
    default_interceptors = interceptors or [access_log_interceptor()]
    bindings: list[ServiceBinding] = []

    if cinematographer is not None:
        bindings.append(ServiceBinding(catalog.cinematographer_contract(), cinematographer))
    if generator is not None:
        bindings.append(ServiceBinding(catalog.generator_contract(), generator))
    if critic is not None:
        bindings.append(ServiceBinding(catalog.critic_contract(), critic))
    if memory is not None:
        bindings.append(ServiceBinding(catalog.memory_contract(), memory))
    if budget is not None:
        bindings.append(ServiceBinding(catalog.budget_contract(), budget))
    if scheduler is not None:
        bindings.append(ServiceBinding(catalog.scheduler_contract(), scheduler))
    if search is not None:
        bindings.append(ServiceBinding(catalog.search_contract(), search))

    mounted: list[str] = []
    for binding in bindings:
        mesh.register(
            binding.contract,
            binding.impl,
            interceptors=list(default_interceptors),
            method_map=binding.method_map,
            health=binding.health,
        )
        mounted.append(binding.contract.name)
    log.info("mesh_mounted_catalog", services=mounted)
    return mounted


def liveness_checker(*, name: str = "self", clock: Any = None) -> HealthChecker:
    """A minimal always-healthy liveness checker (a default for a mounted impl).

    A service with no real dependencies (or whose dependency probes live
    elsewhere) still gets a registry heartbeat via this trivial checker, so it
    appears healthy in :meth:`ServiceMesh.topology`.
    """
    checker = HealthChecker(clock=clock) if clock is not None else HealthChecker()
    checker.add(HealthCheck(name=name, probe=always_healthy()))
    return checker


def callable_health_check(name: str, probe_fn: Any, *, critical: bool = True) -> HealthCheck:
    """Adapt a plain ``async () -> bool`` readiness fn into a :class:`HealthCheck`.

    Lets a package expose its existing readiness probe (e.g. "is the Redis queue
    reachable") without depending on this package's :class:`HealthStatus` enum —
    ``True`` → HEALTHY, ``False`` → UNHEALTHY.
    """

    async def _probe() -> HealthStatus:
        ok = await probe_fn()
        return HealthStatus.HEALTHY if ok else HealthStatus.UNHEALTHY

    return HealthCheck(name=name, probe=_probe, critical=critical)


def fresh_mesh_for_process(
    *, default_timeout_s: float = 5.0, loopback: bool = False
) -> ServiceMesh:
    """Build the per-process mesh the Container would hold (a thin alias).

    Production wires the zero-copy in-process transport (``loopback=False``); a
    split-readiness CI run flips ``loopback=True`` to assert every mounted service
    survives a JSON round-trip.
    """
    return build_default_mesh(default_timeout_s=default_timeout_s, loopback=loopback)


__all__ = [
    "ServiceBinding",
    "callable_health_check",
    "fresh_mesh_for_process",
    "liveness_checker",
    "mount_catalog_services",
]
