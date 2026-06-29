"""Internal RPC + service mesh — the in-process-now / cross-process-later seam.

This package is the substrate that lets Kinora's ~40 backend packages be
*addressed as logical services* over a typed RPC contract while still running in a
single process — and be split out one at a time later **without changing a single
call site** (kinora.md §6: "every agent is an independently deployable service").

The layering, bottom to top:

* **errors** — the gRPC-shaped status taxonomy + retry/transport classification
  (:class:`~app.distributed.rpc.errors.RpcStatus`, :class:`RpcError`).
* **deadline** — injectable :class:`Clock` + propagating :class:`Deadline`
  (the §4.8 time budget that shrinks down a call chain).
* **context** — :class:`RequestContext`: deadline + trace/auth/tenant/idempotency
  propagation across hops (bridged to :mod:`app.telemetry.context`).
* **messages** — the :class:`RpcRequest` / :class:`RpcResponse` wire envelopes.
* **contracts** — the typed in-Python IDL: :class:`ServiceContract` +
  :func:`method`, with a pydantic/dataclass codec.
* **transport** — the seam: :class:`InProcessTransport` (now),
  :class:`LoopbackTransport` (wire-shaped, split-readiness), and
  :class:`DeterministicFakeTransport` (tests, no sockets ever).
* **registry / health** — registration + discovery + active/passive health.
* **loadbalancer / retry / hedging / circuit** — the resilience policies.
* **client / server** — the resilient client and the contract→impl dispatcher.
* **stub** — typed runtime client stubs ("codegen") + an editor-stub emitter.
* **mesh** — :class:`ServiceMesh`, the façade the rest of the backend touches.
* **catalog** — the concrete Kinora service contracts (agents, memory, …).

Importing this package opens no sockets and needs no network.
"""

from __future__ import annotations

from app.distributed.rpc.circuit import (
    BreakerState,
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitConfig,
)
from app.distributed.rpc.client import (
    CallOptions,
    RpcClient,
    TransportResolver,
    constant_transport_resolver,
)
from app.distributed.rpc.context import (
    AuthContext,
    RequestContext,
    context_scope,
    current_context,
    require_context,
)
from app.distributed.rpc.contracts import (
    MethodSpec,
    ServiceContract,
    decode_value,
    encode_value,
    method,
)
from app.distributed.rpc.deadline import (
    Clock,
    Deadline,
    ManualClock,
    SystemClock,
    deadline_for,
)
from app.distributed.rpc.errors import (
    FailureKind,
    RpcError,
    RpcStatus,
)
from app.distributed.rpc.health import (
    HealthCheck,
    HealthChecker,
    HealthReport,
    HealthStatus,
    OutlierConfig,
    OutlierDetector,
)
from app.distributed.rpc.hedging import HedgeBudget, HedgePolicy
from app.distributed.rpc.interceptors import (
    ConcurrencyLimiter,
    RecentCallLog,
    TokenBucketRateLimiter,
    access_log_interceptor,
    require_authenticated,
    require_tenant,
)
from app.distributed.rpc.loadbalancer import (
    InFlightTracker,
    LoadBalancePolicy,
    LoadBalancer,
)
from app.distributed.rpc.mesh import (
    RegisteredService,
    ServiceMesh,
    build_default_mesh,
    build_test_mesh,
)
from app.distributed.rpc.messages import RpcRequest, RpcResponse
from app.distributed.rpc.registry import (
    Discovery,
    InMemoryRegistryStore,
    InstanceHealth,
    RegistryStore,
    ServiceInstance,
    ServiceRegistry,
)
from app.distributed.rpc.retry import (
    BackoffPolicy,
    RetryBudget,
    RetryPolicy,
)
from app.distributed.rpc.server import (
    ServerInterceptor,
    ServiceServer,
    authz_interceptor,
)
from app.distributed.rpc.stub import ServiceStub, generate_stub_source
from app.distributed.rpc.transport import (
    DeterministicFakeTransport,
    Handler,
    InProcessTransport,
    LoopbackTransport,
    RemoteTransportConfig,
    Transport,
)
from app.distributed.rpc.wiring import (
    ServiceBinding,
    callable_health_check,
    mount_catalog_services,
)

__all__ = [
    # errors
    "FailureKind",
    "RpcError",
    "RpcStatus",
    # deadline
    "Clock",
    "Deadline",
    "ManualClock",
    "SystemClock",
    "deadline_for",
    # context
    "AuthContext",
    "RequestContext",
    "context_scope",
    "current_context",
    "require_context",
    # messages
    "RpcRequest",
    "RpcResponse",
    # contracts
    "MethodSpec",
    "ServiceContract",
    "decode_value",
    "encode_value",
    "method",
    # transport
    "DeterministicFakeTransport",
    "Handler",
    "InProcessTransport",
    "LoopbackTransport",
    "RemoteTransportConfig",
    "Transport",
    # registry / discovery
    "Discovery",
    "InMemoryRegistryStore",
    "InstanceHealth",
    "RegistryStore",
    "ServiceInstance",
    "ServiceRegistry",
    # health
    "HealthCheck",
    "HealthChecker",
    "HealthReport",
    "HealthStatus",
    "OutlierConfig",
    "OutlierDetector",
    # load balancing
    "InFlightTracker",
    "LoadBalancePolicy",
    "LoadBalancer",
    # retry
    "BackoffPolicy",
    "RetryBudget",
    "RetryPolicy",
    # hedging
    "HedgeBudget",
    "HedgePolicy",
    # circuit
    "BreakerState",
    "CircuitBreaker",
    "CircuitBreakerRegistry",
    "CircuitConfig",
    # client
    "CallOptions",
    "RpcClient",
    "TransportResolver",
    "constant_transport_resolver",
    # server
    "ServerInterceptor",
    "ServiceServer",
    "authz_interceptor",
    # interceptors
    "ConcurrencyLimiter",
    "RecentCallLog",
    "TokenBucketRateLimiter",
    "access_log_interceptor",
    "require_authenticated",
    "require_tenant",
    # stub
    "ServiceStub",
    "generate_stub_source",
    # mesh
    "RegisteredService",
    "ServiceMesh",
    "build_default_mesh",
    "build_test_mesh",
    # wiring
    "ServiceBinding",
    "callable_health_check",
    "mount_catalog_services",
]
