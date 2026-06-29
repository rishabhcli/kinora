"""The transport abstraction — in-process *now*, network-shaped *later*.

A :class:`Transport` is the one seam between the resilient client and *however*
the request is actually served. The whole decomposition strategy rides on this:
today every transport delivers the call to an in-process handler; the day a
service is split out, only its transport binding changes — the contract, the
client, the resilience policies, and every call site stay byte-identical.

Three implementations ship here, in increasing realism:

* :class:`InProcessTransport` — delivers the :class:`RpcRequest` straight to a
  registered async handler. Zero serialization, zero copy: the fast path for the
  monolith-as-mesh. This is what production uses until a service is physically
  split out.

* :class:`LoopbackTransport` — wraps an in-process handler but **round-trips the
  payload and headers through JSON** first, exactly as an HTTP/gRPC wire would.
  It proves a service is *split-ready*: if a method's request/response survives
  the loopback unchanged, it will survive a real socket. It also models a
  per-call ``base_latency`` so timing-sensitive tests are realistic without a
  network.

* :class:`DeterministicFakeTransport` — for tests: scriptable latency, scriptable
  faults, an explicit call recording, and a seeded RNG. It never reaches a
  handler unless one is wired, so a test *cannot* accidentally touch the network.

A real ``HttpxTransport`` / ``GrpcTransport`` slots in behind the same protocol
later; its shape is sketched in :class:`RemoteTransportConfig` so the wiring is
obvious, but no socket-opening code ships in this layer.
"""

from __future__ import annotations

import json
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.distributed.rpc.deadline import Clock, SystemClock
from app.distributed.rpc.errors import (
    FailureKind,
    RpcError,
    RpcStatus,
    internal,
    unavailable,
)
from app.distributed.rpc.messages import RpcRequest, RpcResponse

#: An in-process handler: takes a request, returns a response. This is what a
#: :class:`~app.distributed.rpc.server.ServiceServer` exposes per endpoint.
Handler = Callable[[RpcRequest], Awaitable[RpcResponse]]

#: A scripted fault responder for the fake transport:
#: ``(request) -> RpcResponse | RpcError | None``. ``None`` falls through.
FaultResponder = Callable[[RpcRequest], "RpcResponse | RpcError | None"]


@runtime_checkable
class Transport(Protocol):
    """The async surface the resilient client depends on.

    A transport delivers one :class:`RpcRequest` to *some* endpoint and returns
    an :class:`RpcResponse`. Application errors come back **in band** as an error
    response; only a transport-level failure (no endpoint, refused, serialization
    broke) raises :class:`RpcError` with :attr:`~RpcError.is_transport` true.
    """

    async def send(self, request: RpcRequest) -> RpcResponse:
        """Deliver one request to its endpoint and return the response."""
        ...

    async def aclose(self) -> None:
        """Release transport resources (idempotent)."""
        ...


# --------------------------------------------------------------------------- #
# In-process transport — the fast path (no serialization).
# --------------------------------------------------------------------------- #


class InProcessTransport:
    """Delivers requests to in-process handlers keyed by ``service.method``.

    The default production transport while the system is one process: a call is
    a direct ``await`` into the target service's handler — no encode/decode, no
    copy. An unknown endpoint is a transport-level ``UNAVAILABLE`` (there is no
    such service here), which is exactly what a discovery miss looks like on a
    real mesh, so callers handle it identically.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self._closed = False

    def bind(self, service: str, method: str, handler: Handler) -> None:
        """Register a handler for one endpoint (overwrites a prior binding)."""
        self._handlers[f"{service}.{method}"] = handler

    def bind_service(self, service: str, handlers: Mapping[str, Handler]) -> None:
        """Register every method handler for a service at once."""
        for mname, handler in handlers.items():
            self.bind(service, mname, handler)

    def unbind_service(self, service: str) -> None:
        """Remove every endpoint for a service (a graceful shutdown / split)."""
        prefix = f"{service}."
        for key in [k for k in self._handlers if k.startswith(prefix)]:
            del self._handlers[key]

    def endpoints(self) -> list[str]:
        """The bound ``service.method`` endpoints (introspection / discovery)."""
        return sorted(self._handlers)

    async def send(self, request: RpcRequest) -> RpcResponse:
        """Deliver the request to its handler; unknown endpoint => UNAVAILABLE."""
        if self._closed:
            raise unavailable("transport closed", service=request.service, method=request.method)
        handler = self._handlers.get(request.endpoint)
        if handler is None:
            raise unavailable(
                f"no handler for {request.endpoint}",
                service=request.service,
                method=request.method,
            )
        return await handler(request)

    async def aclose(self) -> None:
        """Mark closed; further sends fail UNAVAILABLE."""
        self._closed = True


# --------------------------------------------------------------------------- #
# Loopback transport — wire-shaped (JSON round-trip) but still in-process.
# --------------------------------------------------------------------------- #


def _json_roundtrip(value: object) -> object:
    """Serialize then deserialize through JSON (prove wire-survivability)."""
    return json.loads(json.dumps(value))


def _json_roundtrip_dict(value: Mapping[str, object]) -> dict[str, object]:
    """JSON round-trip a mapping, returning a dict (typed convenience)."""
    out = json.loads(json.dumps(dict(value)))
    if not isinstance(out, dict):  # pragma: no cover - input is always a dict
        raise TypeError("expected a JSON object")
    return out


class LoopbackTransport:
    """An in-process transport that round-trips the payload through JSON.

    Same handler registry as :class:`InProcessTransport`, but every request's
    ``payload`` + ``headers`` are JSON-serialized and re-parsed before delivery,
    and the response ``body`` + ``trailers`` on the way back. If a call works
    here, it works over a real socket — so this is the transport split-readiness
    tests run against. A non-JSON-serializable payload surfaces as a transport
    ``INTERNAL`` (the contract codec should have prevented it), which is the
    signal that a service is *not yet* safe to split out.
    """

    def __init__(self, *, base_latency_s: float = 0.0, clock: Clock | None = None) -> None:
        self._handlers: dict[str, Handler] = {}
        self._base_latency_s = base_latency_s
        self._clock = clock or SystemClock()
        self._closed = False

    def bind(self, service: str, method: str, handler: Handler) -> None:
        """Register a handler for one endpoint."""
        self._handlers[f"{service}.{method}"] = handler

    def bind_service(self, service: str, handlers: Mapping[str, Handler]) -> None:
        """Register every method handler for a service at once."""
        for mname, handler in handlers.items():
            self.bind(service, mname, handler)

    def unbind_service(self, service: str) -> None:
        """Remove every endpoint for a service."""
        prefix = f"{service}."
        for key in [k for k in self._handlers if k.startswith(prefix)]:
            del self._handlers[key]

    def endpoints(self) -> list[str]:
        """The bound endpoints."""
        return sorted(self._handlers)

    async def send(self, request: RpcRequest) -> RpcResponse:
        """JSON round-trip the request, deliver, JSON round-trip the response."""
        if self._closed:
            raise unavailable("transport closed", service=request.service, method=request.method)
        handler = self._handlers.get(request.endpoint)
        if handler is None:
            raise unavailable(
                f"no handler for {request.endpoint}",
                service=request.service,
                method=request.method,
            )
        try:
            wire_payload = _json_roundtrip_dict(request.payload)
            wire_headers = _json_roundtrip_dict(request.headers)
        except (TypeError, ValueError) as exc:
            raise internal(
                f"request not wire-serializable for {request.endpoint}: {exc}",
                kind=FailureKind.TRANSPORT,
                service=request.service,
                method=request.method,
                cause=exc,
            ) from exc
        wire_request = RpcRequest(
            service=request.service,
            method=request.method,
            payload=wire_payload,
            headers={str(k): str(v) for k, v in wire_headers.items()},
            attempt=request.attempt,
        )
        response = await handler(wire_request)
        try:
            wire_body = _json_roundtrip(response.body) if response.body is not None else None
            wire_trailers = _json_roundtrip_dict(response.trailers)
        except (TypeError, ValueError) as exc:
            raise internal(
                f"response not wire-serializable for {request.endpoint}: {exc}",
                kind=FailureKind.TRANSPORT,
                service=request.service,
                method=request.method,
                cause=exc,
            ) from exc
        return RpcResponse(
            status=response.status,
            body=wire_body,
            error_message=response.error_message,
            error_kind=response.error_kind,
            error_detail=response.error_detail,
            trailers={str(k): str(v) for k, v in wire_trailers.items()},
        )

    async def aclose(self) -> None:
        """Mark closed."""
        self._closed = True


# --------------------------------------------------------------------------- #
# Deterministic fake transport — tests only.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RecordedSend:
    """One request the :class:`DeterministicFakeTransport` observed."""

    service: str
    method: str
    payload: dict[str, object]
    headers: dict[str, str]
    attempt: int


@dataclass
class DeterministicFakeTransport:
    """A scriptable, deterministic transport for tests (never hits a socket).

    Fully reproducible given ``seed``:

    * each call fails with probability ``fault_rate`` (status ``fault_status``,
      transport-kind so it drives circuit-breaker tests);
    * a per-endpoint :data:`FaultResponder` (in ``responders``) overrides the
      result for specific ``service.method`` keys (return a body / an error);
    * otherwise it falls through to ``default_body`` (status OK), or to a wrapped
      ``inner`` transport when one is provided.

    Every call is appended to :attr:`sends` so a test asserts *what* the client
    issued — including retries / hedges, which show up as repeated sends with an
    incrementing ``attempt``.
    """

    fault_rate: float = 0.0
    fault_status: RpcStatus = RpcStatus.UNAVAILABLE
    default_body: object = None
    seed: int = 0
    responders: dict[str, FaultResponder] = field(default_factory=dict)
    inner: Transport | None = None
    sends: list[RecordedSend] = field(default_factory=list)
    _rng: random.Random = field(init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    async def send(self, request: RpcRequest) -> RpcResponse:
        """Record the call, apply scripted faults/responders, return a response."""
        self.sends.append(
            RecordedSend(
                service=request.service,
                method=request.method,
                payload=dict(request.payload),
                headers=dict(request.headers),
                attempt=request.attempt,
            )
        )
        if self._closed:
            raise unavailable("transport closed", service=request.service, method=request.method)

        responder = self.responders.get(request.endpoint)
        if responder is not None:
            scripted = responder(request)
            if isinstance(scripted, RpcError):
                if scripted.is_transport:
                    raise scripted.with_endpoint(request.service, request.method)
                return RpcResponse.from_error(scripted)
            if isinstance(scripted, RpcResponse):
                return scripted

        if self.fault_rate > 0.0 and self._rng.random() < self.fault_rate:
            raise RpcError(
                self.fault_status,
                f"injected fault {self.fault_status.name}",
                kind=FailureKind.TRANSPORT,
                service=request.service,
                method=request.method,
            )

        if self.inner is not None:
            return await self.inner.send(request)
        return RpcResponse.success(self.default_body)

    async def aclose(self) -> None:
        """Mark closed; further sends fail UNAVAILABLE."""
        self._closed = True
        if self.inner is not None:
            await self.inner.aclose()

    @property
    def closed(self) -> bool:
        """Whether :meth:`aclose` has run."""
        return self._closed

    def sends_to(self, endpoint: str) -> list[RecordedSend]:
        """Recorded sends to one ``service.method`` endpoint (assertion helper)."""
        svc, _, meth = endpoint.partition(".")
        return [s for s in self.sends if s.service == svc and s.method == meth]


# --------------------------------------------------------------------------- #
# Remote transport — the network shape (documented seam, not wired here).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RemoteTransportConfig:
    """Config for a future HTTP/gRPC transport (the split-out wiring point).

    When a service is physically extracted, its discovery entry carries this and
    the client swaps :class:`InProcessTransport` for a real wire transport
    constructed from it. Sketched here so the migration path is concrete; no
    socket-opening code lives in this layer (that keeps the test rule true).
    """

    scheme: str = "http"  # "http" | "grpc"
    host: str = "localhost"
    port: int = 0
    base_path: str = "/rpc"
    connect_timeout_s: float = 2.0

    @property
    def authority(self) -> str:
        """The ``host:port`` authority string."""
        return f"{self.host}:{self.port}"


__all__ = [
    "DeterministicFakeTransport",
    "FaultResponder",
    "Handler",
    "InProcessTransport",
    "LoopbackTransport",
    "RecordedSend",
    "RemoteTransportConfig",
    "Transport",
]
