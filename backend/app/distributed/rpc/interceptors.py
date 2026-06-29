"""Reusable server-side interceptors — cross-cutting concerns, zero impl changes.

The :class:`~app.distributed.rpc.server.ServiceServer` runs a chain of
:data:`~app.distributed.rpc.server.ServerInterceptor` around every method call.
This module ships the production-grade ones so a service gets authz, rate
limiting, server-side deadline shaving, structured access logging, and tenant
isolation by *configuration* — the implementation object never learns they
exist. That separation is the point: when a service is split out, the same
interceptor stack is wired around its new process boundary unchanged.

Each interceptor is a small async function ``(request, ctx, next) -> response``.
They compose outermost-first (the order they're registered), so a typical stack
is ``[access_log, rate_limit, authz, tenant_guard]`` — log everything, shed load
early, then check identity, then scope to a tenant.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.distributed.rpc.context import RequestContext
from app.distributed.rpc.deadline import Clock, SystemClock
from app.distributed.rpc.errors import RpcStatus
from app.distributed.rpc.messages import RpcRequest, RpcResponse
from app.distributed.rpc.server import ServerInterceptor

log = get_logger(__name__)


def access_log_interceptor(*, logger_name: str = "rpc.access") -> ServerInterceptor:
    """Log one structured line per call: endpoint, status, principal, tenant.

    The correlation/trace ids are already bound on the context (the server bound
    them before dispatch), so the line auto-carries them via the structlog
    processor — one log line ties to the whole trace.
    """
    access = get_logger(logger_name)

    async def _intercept(
        request: RpcRequest,
        ctx: RequestContext,
        nxt: Callable[[], Awaitable[RpcResponse]],
    ) -> RpcResponse:
        response = await nxt()
        access.info(
            "rpc_call",
            endpoint=request.endpoint,
            status=response.status.name,
            ok=response.ok,
            principal=ctx.auth.principal,
            tenant=ctx.tenant,
            attempt=request.attempt,
        )
        return response

    return _intercept


def require_authenticated() -> ServerInterceptor:
    """Reject any call whose context carries no authenticated principal."""

    async def _intercept(
        _request: RpcRequest,
        ctx: RequestContext,
        nxt: Callable[[], Awaitable[RpcResponse]],
    ) -> RpcResponse:
        if not ctx.auth.is_authenticated:
            return RpcResponse.failure(RpcStatus.UNAUTHENTICATED, "authentication required")
        return await nxt()

    return _intercept


def require_tenant() -> ServerInterceptor:
    """Reject any call that arrives without a tenant (multi-tenant isolation)."""

    async def _intercept(
        _request: RpcRequest,
        ctx: RequestContext,
        nxt: Callable[[], Awaitable[RpcResponse]],
    ) -> RpcResponse:
        if not ctx.tenant:
            return RpcResponse.failure(
                RpcStatus.INVALID_ARGUMENT, "a tenant is required for this service"
            )
        return await nxt()

    return _intercept


@dataclass
class TokenBucketRateLimiter:
    """A deterministic token-bucket rate limiter (per a chosen key).

    ``rate`` tokens accrue per second up to ``burst``; each call spends one.
    Refill is computed against the injected clock, so the limiter is exact and
    testable with a :class:`ManualClock` — no background timer. The bucket key is
    derived per call (default: the tenant, falling back to the principal), so one
    noisy tenant can't starve the others (§12.2 per-session fairness, generalized).
    """

    rate: float = 50.0
    burst: float = 100.0
    clock: Clock = field(default_factory=SystemClock)
    _buckets: dict[str, tuple[float, float]] = field(default_factory=dict)  # key -> (tokens, ts)

    def _key(self, ctx: RequestContext) -> str:
        return ctx.tenant or ctx.auth.principal or "anonymous"

    def allow(self, ctx: RequestContext) -> bool:
        """Whether a call from this context may proceed (spends one token)."""
        key = self._key(ctx)
        now = self.clock.now()
        tokens, ts = self._buckets.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - ts) * self.rate)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True

    def interceptor(self) -> ServerInterceptor:
        """A :class:`ServerInterceptor` that sheds over-rate calls early."""

        async def _intercept(
            _request: RpcRequest,
            ctx: RequestContext,
            nxt: Callable[[], Awaitable[RpcResponse]],
        ) -> RpcResponse:
            if not self.allow(ctx):
                return RpcResponse.failure(
                    RpcStatus.RESOURCE_EXHAUSTED,
                    "rate limit exceeded",
                    detail={"key": self._key(ctx)},
                )
            return await nxt()

        return _intercept


@dataclass
class ConcurrencyLimiter:
    """A per-service in-flight cap (bulkhead) — shed load past ``max_in_flight``.

    Prevents one slow downstream from consuming unbounded server concurrency
    (the §12.2 backpressure idea applied at the service boundary). Strictly an
    accounting limiter (inc on entry, dec on exit); pairs with the client-side
    deadline so a shed call fails fast rather than queueing unbounded.
    """

    max_in_flight: int = 64
    _in_flight: int = field(default=0, init=False)
    rejected: int = field(default=0, init=False)

    def interceptor(self) -> ServerInterceptor:
        """A :class:`ServerInterceptor` enforcing the in-flight cap."""

        async def _intercept(
            _request: RpcRequest,
            _ctx: RequestContext,
            nxt: Callable[[], Awaitable[RpcResponse]],
        ) -> RpcResponse:
            if self._in_flight >= self.max_in_flight:
                self.rejected += 1
                return RpcResponse.failure(
                    RpcStatus.RESOURCE_EXHAUSTED, "service at concurrency limit"
                )
            self._in_flight += 1
            try:
                return await nxt()
            finally:
                self._in_flight -= 1

        return _intercept


@dataclass
class RecentCallLog:
    """A bounded ring of recent calls (a lightweight server-side audit trail)."""

    capacity: int = 256
    _entries: deque[tuple[str, str, str | None]] = field(default_factory=deque)

    def interceptor(self) -> ServerInterceptor:
        """An interceptor that records ``(endpoint, status, principal)`` per call."""

        async def _intercept(
            request: RpcRequest,
            ctx: RequestContext,
            nxt: Callable[[], Awaitable[RpcResponse]],
        ) -> RpcResponse:
            response = await nxt()
            self._entries.append((request.endpoint, response.status.name, ctx.auth.principal))
            while len(self._entries) > self.capacity:
                self._entries.popleft()
            return response

        return _intercept

    def recent(self) -> list[tuple[str, str, str | None]]:
        """The recorded calls, oldest first."""
        return list(self._entries)


__all__ = [
    "ConcurrencyLimiter",
    "RecentCallLog",
    "TokenBucketRateLimiter",
    "access_log_interceptor",
    "require_authenticated",
    "require_tenant",
]
