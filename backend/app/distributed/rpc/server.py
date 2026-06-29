"""The service server — bind a contract to an implementation and dispatch.

A :class:`ServiceServer` is the server-side mirror of the client: it takes a
:class:`ServiceContract` and an *implementation object* (any object with an async
method per contract method) and produces one transport :class:`Handler` per
endpoint. Each handler does the unglamorous-but-essential server work:

1. **rehydrate the request context** from the wire headers (trace/auth/tenant/
   deadline) and bind it as ambient, so the implementation's logs + any nested
   RPCs it makes inherit the trace and the *shrinking* deadline (§4.8);
2. **enforce the deadline** before running — a request that already blew its
   budget in transit is rejected ``DEADLINE_EXCEEDED`` without doing work;
3. **decode** the wire payload to the contract's typed request and **encode** the
   typed response back — validation failures are clean ``INVALID_ARGUMENT``;
4. **dedup by idempotency key** so a duplicate Scheduler event / client retry
   collapses to one execution and returns the first result (§12.1 — "re-enqueuing
   the same shot is a no-op that returns the existing result");
5. **normalise errors**: an ``RpcError`` the impl raises passes through with its
   status; any other exception becomes ``INTERNAL`` (the server never leaks a
   stack trace across the seam).

The implementation object stays a plain Python class — it never imports anything
from this layer. That is the decomposition seam: the *same* impl runs in-process
today and behind a socket tomorrow, with only its binding changing.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.distributed.rpc.context import RequestContext, context_scope
from app.distributed.rpc.contracts import MethodSpec, ServiceContract
from app.distributed.rpc.deadline import Clock, SystemClock
from app.distributed.rpc.errors import (
    RpcError,
    RpcStatus,
    deadline_exceeded,
    internal,
    unimplemented,
)
from app.distributed.rpc.messages import RpcRequest, RpcResponse
from app.distributed.rpc.transport import Handler

log = get_logger(__name__)

#: A server-side interceptor: wraps the next handler. Chained around dispatch for
#: cross-cutting concerns (authz, rate-limit, logging) without touching the impl.
ServerInterceptor = Callable[
    [RpcRequest, RequestContext, "Callable[[], Awaitable[RpcResponse]]"],
    Awaitable[RpcResponse],
]


@dataclass
class _IdempotencyCache:
    """A bounded LRU of completed results keyed by ``endpoint + idempotency_key``.

    Returns the *first* response for a duplicate request so a retried / duplicated
    write is a no-op that replays the original result (§12.1). Bounded so a churn
    of keys can't grow without limit; in-process only (a distributed dedup would
    back this with Redis behind the same interface).
    """

    capacity: int = 1024
    _entries: dict[str, RpcResponse] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)

    def get(self, key: str) -> RpcResponse | None:
        """Return a cached response for ``key`` (``None`` on miss)."""
        return self._entries.get(key)

    def put(self, key: str, response: RpcResponse) -> None:
        """Cache ``response`` under ``key``, evicting the oldest when full."""
        if key in self._entries:
            self._entries[key] = response
            return
        if len(self._order) >= self.capacity:
            oldest = self._order.pop(0)
            self._entries.pop(oldest, None)
        self._entries[key] = response
        self._order.append(key)


@dataclass
class ServiceServer:
    """Binds a contract + implementation into per-endpoint transport handlers.

    Register the server's handlers on a transport with
    :meth:`InProcessTransport.bind_service(server.service_name, server.handlers())`.
    The implementation may expose each method as ``async def <method>(request)``
    (the typed request the contract decodes), or override the mapping with a
    ``method_map`` for legacy names.
    """

    contract: ServiceContract
    impl: Any
    clock: Clock = field(default_factory=SystemClock)
    interceptors: list[ServerInterceptor] = field(default_factory=list)
    method_map: dict[str, str] = field(default_factory=dict)
    enforce_deadline: bool = True
    _dedup: _IdempotencyCache = field(default_factory=_IdempotencyCache, init=False)

    @property
    def service_name(self) -> str:
        """The logical service name this server exposes."""
        return self.contract.name

    def _resolve_impl_method(self, method: MethodSpec) -> Callable[..., Any]:
        """Find the impl callable for a contract method (or raise UNIMPLEMENTED)."""
        attr = self.method_map.get(method.name, method.name)
        fn = getattr(self.impl, attr, None)
        if fn is None or not callable(fn):
            raise unimplemented(
                f"{self.contract.name}.{method.name} has no implementation",
                service=self.contract.name,
                method=method.name,
            )
        return fn

    def handlers(self) -> dict[str, Handler]:
        """Return a transport handler per contract method (bind these)."""
        return {name: self._make_handler(spec) for name, spec in self.contract.methods.items()}

    def _make_handler(self, spec: MethodSpec) -> Handler:
        async def _handle(request: RpcRequest) -> RpcResponse:
            ctx = RequestContext.from_headers(request.headers, clock=self.clock)
            with context_scope(ctx):
                return await self._dispatch(request, spec, ctx)

        return _handle

    async def _dispatch(
        self, request: RpcRequest, spec: MethodSpec, ctx: RequestContext
    ) -> RpcResponse:
        """Run the full server pipeline for one request."""
        # 1. Deadline already blown in transit?
        if self.enforce_deadline and ctx.expired(clock=self.clock):
            return RpcResponse.from_error(
                deadline_exceeded(
                    "request deadline expired before processing",
                    service=self.contract.name,
                    method=spec.name,
                )
            )

        # 2. Idempotency replay (only for idempotent methods carrying a key).
        dedup_key: str | None = None
        if spec.idempotent and ctx.idempotency_key:
            dedup_key = f"{self.contract.name}.{spec.name}#{ctx.idempotency_key}"
            cached = self._dedup.get(dedup_key)
            if cached is not None:
                return RpcResponse(
                    status=cached.status,
                    body=cached.body,
                    error_message=cached.error_message,
                    error_kind=cached.error_kind,
                    error_detail=cached.error_detail,
                    trailers={**cached.trailers, "idempotent_replay": "true"},
                )

        async def _core() -> RpcResponse:
            return await self._invoke_impl(request, spec)

        # 3. Interceptor chain (authz / rate-limit / logging), innermost = _core.
        handler: Callable[[], Awaitable[RpcResponse]] = _core
        for interceptor in reversed(self.interceptors):
            handler = self._wrap(interceptor, request, ctx, handler)
        response = await handler()

        # 4. Cache a successful idempotent result for replay.
        if dedup_key is not None and response.ok:
            self._dedup.put(dedup_key, response)
        return response

    @staticmethod
    def _wrap(
        interceptor: ServerInterceptor,
        request: RpcRequest,
        ctx: RequestContext,
        nxt: Callable[[], Awaitable[RpcResponse]],
    ) -> Callable[[], Awaitable[RpcResponse]]:
        async def _wrapped() -> RpcResponse:
            return await interceptor(request, ctx, nxt)

        return _wrapped

    async def _invoke_impl(self, request: RpcRequest, spec: MethodSpec) -> RpcResponse:
        """Decode → call impl → encode, normalising every error."""
        try:
            typed_request = spec.decode_request(request.payload)
        except RpcError as err:
            return RpcResponse.from_error(err)

        fn = self._resolve_impl_method(spec)
        try:
            if typed_request is None and not _takes_argument(fn):
                result = fn()
            else:
                result = fn(typed_request)
            if inspect.isawaitable(result):
                result = await result
        except RpcError as err:
            return RpcResponse.from_error(err.with_endpoint(self.contract.name, spec.name))
        except Exception as exc:  # never leak a stack trace across the seam
            log.warning(
                "rpc_impl_error",
                service=self.contract.name,
                method=spec.name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return RpcResponse.from_error(
                internal(
                    f"unhandled error in {self.contract.name}.{spec.name}: {exc}",
                    service=self.contract.name,
                    method=spec.name,
                    cause=exc,
                )
            )

        try:
            body = spec.encode_response(result)
        except RpcError as err:
            return RpcResponse.from_error(err)
        return RpcResponse.success(body, trailers={"served_by": self.contract.name})


def _takes_argument(fn: Callable[..., Any]) -> bool:
    """Whether ``fn`` accepts at least one positional argument (the request)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    for param in sig.parameters.values():
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            return True
    return False


def authz_interceptor(required_scope: str) -> ServerInterceptor:
    """An interceptor that rejects calls whose context lacks ``required_scope``.

    Demonstrates the server interceptor seam: cross-cutting authz with no change
    to the implementation. A missing scope is ``PERMISSION_DENIED``.
    """

    async def _intercept(
        _request: RpcRequest,
        ctx: RequestContext,
        nxt: Callable[[], Awaitable[RpcResponse]],
    ) -> RpcResponse:
        if not ctx.auth.has_scope(required_scope):
            return RpcResponse.failure(
                RpcStatus.PERMISSION_DENIED,
                f"missing required scope {required_scope!r}",
            )
        return await nxt()

    return _intercept


__all__ = [
    "ServerInterceptor",
    "ServiceServer",
    "authz_interceptor",
]
