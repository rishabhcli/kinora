"""The service mesh façade — address the monolith's packages as logical services.

:class:`ServiceMesh` is the one object the rest of the backend touches. It wires
together every part of this package — a transport, a registry + discovery, a
resilient client — and exposes two verbs:

* :meth:`register` — a package hands the mesh a :class:`ServiceContract` and an
  *implementation object* (a plain class — e.g. the Cinematographer agent, the
  budget service, the search service). The mesh binds a
  :class:`~app.distributed.rpc.server.ServiceServer` onto the transport and
  records an in-process instance in the registry. The package's code is unchanged.

* :meth:`stub` — any caller asks the mesh for a typed
  :class:`~app.distributed.rpc.stub.ServiceStub` for a service name and calls its
  methods. The call goes contract-encode → client (deadline/retry/hedge/breaker/
  load-balance) → transport → server → impl, and back — all in-process today.

That is the whole decomposition strategy in one object: today every registered
service shares one :class:`InProcessTransport`, so a stub call is a direct
``await`` with the full resilience stack around it; the day a service is split
out, its registration moves to a separate process and its registry entry points at
a remote transport — **no call site changes**. The façade is the seam that makes
"40 packages" and "40 services" the same code.

The :func:`build_default_mesh` factory returns a mesh ready for the in-process
default (loopback or direct transport, in-memory registry, sensible policies),
and :func:`build_test_mesh` returns one wired with a :class:`ManualClock` + an
immediate ``sleep`` so the whole stack is deterministic in tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.distributed.rpc.client import (
    CallOptions,
    RpcClient,
    constant_transport_resolver,
)
from app.distributed.rpc.context import RequestContext
from app.distributed.rpc.contracts import ServiceContract
from app.distributed.rpc.deadline import Clock, ManualClock, SystemClock
from app.distributed.rpc.health import HealthChecker, HealthReport, HealthStatus
from app.distributed.rpc.loadbalancer import LoadBalancePolicy
from app.distributed.rpc.registry import (
    Discovery,
    InstanceHealth,
    ServiceRegistry,
)
from app.distributed.rpc.retry import RetryPolicy
from app.distributed.rpc.server import ServerInterceptor, ServiceServer
from app.distributed.rpc.stub import ServiceStub
from app.distributed.rpc.transport import (
    InProcessTransport,
    LoopbackTransport,
    Transport,
)

log = get_logger(__name__)


@dataclass
class RegisteredService:
    """Bookkeeping for one service registered with the mesh."""

    contract: ServiceContract
    server: ServiceServer
    instance_id: str
    health: HealthChecker | None = None


@dataclass
class ServiceMesh:
    """The wired façade: a transport + registry + discovery + resilient client.

    Construct via :func:`build_default_mesh` / :func:`build_test_mesh` rather than
    directly so the policy defaults + clock are coherent.
    """

    transport: Transport
    registry: ServiceRegistry
    discovery: Discovery
    client: RpcClient
    clock: Clock = field(default_factory=SystemClock)
    default_timeout_s: float = 5.0
    _services: dict[str, RegisteredService] = field(default_factory=dict, init=False)
    _stubs: dict[str, ServiceStub] = field(default_factory=dict, init=False)

    # -- registration (the write side) ------------------------------------- #

    def register(
        self,
        contract: ServiceContract,
        impl: Any,
        *,
        instance_id: str | None = None,
        interceptors: list[ServerInterceptor] | None = None,
        method_map: Mapping[str, str] | None = None,
        health: HealthChecker | None = None,
        weight: int = 1,
        zone: str = "local",
        metadata: Mapping[str, str] | None = None,
    ) -> RegisteredService:
        """Bind a package's contract + impl as an in-process logical service.

        Idempotent on the contract (fingerprint-checked). Registers the contract,
        binds a :class:`ServiceServer`'s handlers on the shared transport, and
        records an in-process instance so discovery resolves it. Returns the
        bookkeeping record (also retrievable via :meth:`registered`).
        """
        self.registry.register_contract(contract)
        server = ServiceServer(
            contract=contract,
            impl=impl,
            clock=self.clock,
            interceptors=list(interceptors or []),
            method_map=dict(method_map or {}),
        )
        # Bind every method handler on the transport (must support binding).
        binder = getattr(self.transport, "bind_service", None)
        if binder is None:
            raise TypeError(
                f"transport {type(self.transport).__name__} cannot host services "
                "(no bind_service); use InProcessTransport / LoopbackTransport"
            )
        binder(contract.name, server.handlers())

        iid = instance_id or f"{contract.name}-local-0"
        self.registry.register_instance(
            contract.name,
            iid,
            version=contract.version,
            transport_ref="in-process",
            weight=weight,
            zone=zone,
            metadata=dict(metadata or {}),
        )
        record = RegisteredService(
            contract=contract, server=server, instance_id=iid, health=health
        )
        self._services[contract.name] = record
        self._stubs.pop(contract.name, None)  # invalidate any cached stub
        log.info(
            "mesh_register",
            service=contract.name,
            version=contract.version,
            methods=sorted(contract.methods),
            fingerprint=contract.fingerprint(),
        )
        return record

    def deregister(self, service: str) -> None:
        """Remove a service (graceful shutdown / before a real split-out)."""
        record = self._services.pop(service, None)
        if record is None:
            return
        unbinder = getattr(self.transport, "unbind_service", None)
        if unbinder is not None:
            unbinder(service)
        self.registry.deregister(service, record.instance_id)
        self._stubs.pop(service, None)

    def registered(self, service: str) -> RegisteredService | None:
        """The bookkeeping record for a registered service (``None`` if absent)."""
        return self._services.get(service)

    def services(self) -> list[str]:
        """Every logical service currently registered with the mesh."""
        return sorted(self._services)

    # -- access (the read side) -------------------------------------------- #

    def stub(self, service: str) -> ServiceStub:
        """Return (caching) a typed stub for a registered service.

        Raises ``KeyError`` if the service was never registered — a typo'd service
        name fails loudly at the call site rather than producing UNAVAILABLE at
        send time.
        """
        cached = self._stubs.get(service)
        if cached is not None:
            return cached
        record = self._services.get(service)
        if record is None:
            raise KeyError(
                f"service {service!r} is not registered with the mesh; "
                f"known: {self.services()}"
            )
        stub = ServiceStub(record.contract, self.client)
        self._stubs[service] = stub
        return stub

    def new_context(
        self,
        *,
        timeout_s: float | None = None,
        principal: str | None = None,
        token: str | None = None,
        scopes: tuple[str, ...] = (),
        tenant: str | None = None,
        idempotency_key: str | None = None,
        baggage: Mapping[str, str] | None = None,
    ) -> RequestContext:
        """Start a root :class:`RequestContext` for an originating call.

        Convenience that pins the mesh clock + default timeout so a caller at the
        edge (an HTTP handler, the Scheduler) gets a correctly-budgeted context
        without importing the context module.
        """
        return RequestContext.root(
            clock=self.clock,
            timeout_s=timeout_s if timeout_s is not None else self.default_timeout_s,
            principal=principal,
            token=token,
            scopes=scopes,
            tenant=tenant,
            idempotency_key=idempotency_key,
            baggage=baggage,
        )

    async def call(
        self,
        service: str,
        method: str,
        request: Any = None,
        *,
        context: RequestContext | None = None,
        options: CallOptions | None = None,
    ) -> Any:
        """One-shot typed call (resolves the stub for you).

        Equivalent to ``await self.stub(service).invoke(method, request, …)`` with
        a fresh default context when none is supplied. The ergonomic entry point
        for an edge caller that just wants a result.
        """
        ctx = context or self.new_context()
        return await self.stub(service).invoke(
            method, request, context=ctx, options=options
        )

    # -- health (the operability side) ------------------------------------- #

    async def check_health(self, service: str) -> HealthReport:
        """Run a registered service's health checks and update the registry.

        The aggregate verdict is written back as the instance's heartbeat so
        discovery routes around an unhealthy service automatically. A service
        without a registered checker is reported healthy (nothing to fail).
        """
        record = self._services.get(service)
        if record is None:
            return HealthReport(status=HealthStatus.UNHEALTHY)
        if record.health is None:
            self.registry.heartbeat(service, record.instance_id, healthy=True)
            return HealthReport(status=HealthStatus.HEALTHY)
        report = await record.health.run()
        self.registry.heartbeat(
            service,
            record.instance_id,
            healthy=report.status is not HealthStatus.UNHEALTHY,
            status=_to_instance_health(report.status),
        )
        return report

    async def check_all(self) -> dict[str, HealthReport]:
        """Run health checks for every registered service."""
        out: dict[str, HealthReport] = {}
        for service in self.services():
            out[service] = await self.check_health(service)
        return out

    def topology(self) -> dict[str, Any]:
        """A JSON-ready snapshot of the mesh (services, instances, breakers).

        Powers an operability view / a mesh dashboard: who is registered, what
        each instance's health is, and which circuit breakers are open.
        """
        return {
            "services": [
                {
                    "name": record.contract.name,
                    "version": record.contract.version,
                    "fingerprint": record.contract.fingerprint(),
                    "methods": sorted(record.contract.methods),
                    "instances": [
                        {
                            "instance_id": inst.instance_id,
                            "healthy": inst.health,
                            "status": inst.health_status.value,
                            "zone": inst.zone,
                            "weight": inst.weight,
                        }
                        for inst in self.discovery.resolve(
                            record.contract.name, include_unhealthy=True
                        )
                    ],
                }
                for record in self._services.values()
            ],
            "breakers": {ep: state.value for ep, state in self.client.breakers.states().items()},
            "ejected_instances": self.client.outliers.ejected_instances(),
        }


def _to_instance_health(status: HealthStatus) -> InstanceHealth:
    return status.to_instance_health()


# --------------------------------------------------------------------------- #
# Factories.
# --------------------------------------------------------------------------- #


def build_default_mesh(
    *,
    transport: Transport | None = None,
    clock: Clock | None = None,
    default_timeout_s: float = 5.0,
    lb_policy: LoadBalancePolicy = LoadBalancePolicy.P2C,
    retry: RetryPolicy | None = None,
    loopback: bool = False,
) -> ServiceMesh:
    """Build an in-process mesh with sensible defaults.

    ``loopback=True`` wires the JSON-round-tripping :class:`LoopbackTransport`
    (proves split-readiness); otherwise the zero-copy :class:`InProcessTransport`.
    A custom ``transport`` overrides both. The client's resilience defaults are
    conservative (3 attempts, P2C balancing, breakers on) and overridable per call.
    """
    clk = clock or SystemClock()
    tport: Transport = transport or (
        LoopbackTransport(clock=clk) if loopback else InProcessTransport()
    )
    registry = ServiceRegistry(clock=clk)
    discovery = Discovery(registry=registry)
    client = RpcClient(
        discovery=discovery,
        transport_resolver=constant_transport_resolver(tport),
        clock=clk,
        default_timeout_s=default_timeout_s,
        default_retry=retry or RetryPolicy(),
        default_lb_policy=lb_policy,
    )
    return ServiceMesh(
        transport=tport,
        registry=registry,
        discovery=discovery,
        client=client,
        clock=clk,
        default_timeout_s=default_timeout_s,
    )


def build_test_mesh(
    *,
    clock: ManualClock | None = None,
    default_timeout_s: float = 5.0,
    loopback: bool = True,
    retry: RetryPolicy | None = None,
) -> ServiceMesh:
    """Build a fully-deterministic mesh for tests.

    Wires a :class:`ManualClock` and an *immediate* sleep seam that advances that
    clock instead of waiting, so retry backoff / hedge delays / deadlines are
    exercised without any real time passing. Defaults to the loopback transport so
    tests also assert wire-survivability for free.
    """
    clk = clock or ManualClock()
    mesh = build_default_mesh(
        clock=clk,
        default_timeout_s=default_timeout_s,
        retry=retry,
        loopback=loopback,
    )

    async def _advancing_sleep(seconds: float) -> None:
        if seconds > 0:
            clk.advance(seconds)

    mesh.client.sleep = _advancing_sleep
    return mesh


__all__ = [
    "RegisteredService",
    "ServiceMesh",
    "build_default_mesh",
    "build_test_mesh",
]
