"""Service registry + discovery seam — where logical services resolve to instances.

A caller names a *logical* service (``"cinematographer"``); discovery turns that
into a concrete set of :class:`ServiceInstance` endpoints with health and weight.
Today every instance is in-process (one endpoint, the local handler); tomorrow a
split-out service has N remote instances. The caller's code never changes —
only what discovery returns.

Two seams, deliberately separated:

* **registry** (:class:`ServiceRegistry`) — the *write* side: a service
  *registers* its instances + the :class:`~app.distributed.rpc.contracts.ServiceContract`
  it serves and heartbeats them. In-process this is a dict; in production it's
  backed by Consul / etcd / a Redis registry behind the same :class:`RegistryStore`
  protocol (a stub Redis-shaped store is sketched, not wired).

* **discovery** (:class:`Discovery`) — the *read* side the client depends on:
  ``resolve(service) -> [instances]``. It filters to the requested
  ``min_version``, honours TTL'd heartbeats (a stale instance is treated as
  unhealthy), and is the single place the load balancer and circuit breaker pull
  their candidate set from.

Health flows in from :mod:`app.distributed.rpc.health`; the registry just stores
the last reported status + a heartbeat timestamp, and discovery applies the TTL.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from app.distributed.rpc.contracts import ServiceContract
from app.distributed.rpc.deadline import Clock, SystemClock


class InstanceHealth(enum.Enum):
    """The coarse health of one instance (set by the health checker)."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ServiceInstance:
    """One concrete endpoint serving a logical service.

    ``transport_ref`` is an opaque pointer the client uses to obtain the right
    :class:`~app.distributed.rpc.transport.Transport` for this instance: for an
    in-process instance it's just the service name (the local transport routes by
    ``service.method``); for a remote instance it would carry host:port. ``weight``
    biases weighted policies; ``zone`` enables zone-aware routing later.
    """

    service: str
    instance_id: str
    version: int = 1
    transport_ref: str = "in-process"
    weight: int = 1
    zone: str = "local"
    health: bool = True
    health_status: InstanceHealth = InstanceHealth.HEALTHY
    last_heartbeat: float = 0.0
    metadata: Mapping[str, str] = field(default_factory=dict)

    def with_health(
        self, *, healthy: bool, status: InstanceHealth, at: float
    ) -> ServiceInstance:
        """Return a copy with updated health + heartbeat timestamp."""
        return replace(self, health=healthy, health_status=status, last_heartbeat=at)


@runtime_checkable
class RegistryStore(Protocol):
    """The pluggable backing store for registrations (in-memory / Redis / etcd).

    Kept narrow so a real distributed store drops in behind it without touching
    the registry/discovery logic. A production Redis-backed store would implement
    these over hashes + a heartbeat key with PEXPIRE; that store is intentionally
    *not* shipped here (it needs infra), but the seam is.
    """

    def put(self, instance: ServiceInstance) -> None:
        """Insert or replace an instance registration."""
        ...

    def remove(self, service: str, instance_id: str) -> None:
        """Delete an instance registration."""
        ...

    def list_service(self, service: str) -> list[ServiceInstance]:
        """All instances registered for a logical service."""
        ...

    def services(self) -> list[str]:
        """Every distinct logical service name with a registration."""
        ...


@dataclass
class InMemoryRegistryStore:
    """The default in-process registry store (a nested dict)."""

    _by_service: dict[str, dict[str, ServiceInstance]] = field(default_factory=dict)

    def put(self, instance: ServiceInstance) -> None:
        """Insert or replace an instance registration."""
        self._by_service.setdefault(instance.service, {})[instance.instance_id] = instance

    def remove(self, service: str, instance_id: str) -> None:
        """Delete an instance registration (no-op if absent)."""
        bucket = self._by_service.get(service)
        if bucket is not None:
            bucket.pop(instance_id, None)
            if not bucket:
                self._by_service.pop(service, None)

    def list_service(self, service: str) -> list[ServiceInstance]:
        """All instances registered for a logical service."""
        return list(self._by_service.get(service, {}).values())

    def services(self) -> list[str]:
        """Every distinct logical service name with a registration."""
        return sorted(self._by_service)


@dataclass
class ServiceRegistry:
    """The write side: register services + their contracts, heartbeat instances.

    A service registers once with its :class:`ServiceContract` (so discovery can
    expose the method surface + fingerprint) and one or more instances. The
    contract is stored alongside the instances; a second registration of the same
    service must present a contract with a matching fingerprint, or it's rejected
    — that's the guard against two nodes disagreeing on the wire shape.
    """

    store: RegistryStore = field(default_factory=InMemoryRegistryStore)
    clock: Clock = field(default_factory=SystemClock)
    _contracts: dict[str, ServiceContract] = field(default_factory=dict)

    def register_contract(self, contract: ServiceContract) -> None:
        """Record the contract a service serves (idempotent; fingerprint-checked)."""
        existing = self._contracts.get(contract.name)
        if existing is not None and existing.fingerprint() != contract.fingerprint():
            raise ValueError(
                f"contract conflict for {contract.name!r}: "
                f"{existing.fingerprint()} != {contract.fingerprint()}"
            )
        self._contracts[contract.name] = contract

    def contract(self, service: str) -> ServiceContract | None:
        """The registered contract for a service (``None`` if unregistered)."""
        return self._contracts.get(service)

    def register_instance(
        self,
        service: str,
        instance_id: str,
        *,
        version: int = 1,
        transport_ref: str = "in-process",
        weight: int = 1,
        zone: str = "local",
        metadata: Mapping[str, str] | None = None,
    ) -> ServiceInstance:
        """Register (or refresh) one instance; stamps the initial heartbeat."""
        instance = ServiceInstance(
            service=service,
            instance_id=instance_id,
            version=version,
            transport_ref=transport_ref,
            weight=weight,
            zone=zone,
            health=True,
            health_status=InstanceHealth.HEALTHY,
            last_heartbeat=self.clock.now(),
            metadata=dict(metadata or {}),
        )
        self.store.put(instance)
        return instance

    def heartbeat(
        self,
        service: str,
        instance_id: str,
        *,
        healthy: bool = True,
        status: InstanceHealth = InstanceHealth.HEALTHY,
    ) -> None:
        """Refresh an instance's heartbeat + health (the liveness signal)."""
        for inst in self.store.list_service(service):
            if inst.instance_id == instance_id:
                self.store.put(
                    inst.with_health(healthy=healthy, status=status, at=self.clock.now())
                )
                return

    def deregister(self, service: str, instance_id: str) -> None:
        """Remove an instance (graceful shutdown / scale-in)."""
        self.store.remove(service, instance_id)


@dataclass
class Discovery:
    """The read side the client depends on: resolve a service to instances.

    Applies the heartbeat ``ttl_s``: an instance whose last heartbeat is older
    than the TTL is treated as unhealthy regardless of its stored status (it has
    gone silent). Filters by ``min_version`` so a caller pinned to a contract
    version never routes to an older incompatible instance.
    """

    registry: ServiceRegistry
    ttl_s: float = 30.0

    def resolve(
        self,
        service: str,
        *,
        min_version: int = 1,
        include_unhealthy: bool = False,
    ) -> list[ServiceInstance]:
        """Return the candidate instances for a logical service.

        Stale (TTL-expired) instances are marked unhealthy. By default only
        healthy instances are returned; ``include_unhealthy`` returns all for
        introspection / health dashboards.
        """
        now = self.registry.clock.now()
        out: list[ServiceInstance] = []
        for inst in self.registry.store.list_service(service):
            if inst.version < min_version:
                continue
            # The registry always stamps last_heartbeat on register/heartbeat, so
            # the age is well-defined even at t==0 (a registration at the clock
            # origin). A negative-infinite TTL never expires.
            stale = (
                self.ttl_s >= 0.0 and (now - inst.last_heartbeat) > self.ttl_s
            )
            healthy = inst.health and not stale
            resolved = inst if not stale else inst.with_health(
                healthy=False, status=InstanceHealth.UNHEALTHY, at=inst.last_heartbeat
            )
            if healthy or include_unhealthy:
                out.append(resolved)
        return out

    def has_service(self, service: str) -> bool:
        """Whether any (registered) instance exists for the service."""
        return bool(self.registry.store.list_service(service))

    def services(self) -> list[str]:
        """Every known logical service name."""
        return self.registry.store.services()


__all__ = [
    "Discovery",
    "InMemoryRegistryStore",
    "InstanceHealth",
    "RegistryStore",
    "ServiceInstance",
    "ServiceRegistry",
]
