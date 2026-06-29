"""Tests for the reusable server-side interceptors."""

from __future__ import annotations

from dataclasses import dataclass

from app.distributed.rpc.context import AuthContext, RequestContext
from app.distributed.rpc.contracts import ServiceContract, method
from app.distributed.rpc.deadline import ManualClock
from app.distributed.rpc.errors import RpcStatus
from app.distributed.rpc.interceptors import (
    ConcurrencyLimiter,
    RecentCallLog,
    TokenBucketRateLimiter,
    access_log_interceptor,
    require_authenticated,
    require_tenant,
)
from app.distributed.rpc.messages import RpcRequest
from app.distributed.rpc.server import ServiceServer


@dataclass
class Req:
    x: int


@dataclass
class Resp:
    y: int


class Impl:
    async def go(self, req: Req) -> Resp:
        return Resp(y=req.x)


def _contract() -> ServiceContract:
    return ServiceContract.define("svc", methods=[method("go", Req, Resp)])


def _server(clk: ManualClock, *interceptors: object) -> ServiceServer:
    return ServiceServer(
        contract=_contract(), impl=Impl(), clock=clk, interceptors=list(interceptors)  # type: ignore[arg-type]
    )


def _req(headers: dict[str, str] | None = None) -> RpcRequest:
    return RpcRequest("svc", "go", payload={"x": 1}, headers=headers or {})


async def test_require_authenticated_rejects_anonymous() -> None:
    clk = ManualClock()
    server = _server(clk, require_authenticated())
    resp = await server.handlers()["go"](_req())
    assert resp.status is RpcStatus.UNAUTHENTICATED


async def test_require_authenticated_allows_principal() -> None:
    clk = ManualClock()
    server = _server(clk, require_authenticated())
    ctx = RequestContext.root(clock=clk).with_auth(AuthContext(principal="u"))
    resp = await server.handlers()["go"](_req(ctx.to_headers(clock=clk)))
    assert resp.ok


async def test_require_tenant_rejects_missing() -> None:
    clk = ManualClock()
    server = _server(clk, require_tenant())
    resp = await server.handlers()["go"](_req())
    assert resp.status is RpcStatus.INVALID_ARGUMENT


async def test_rate_limiter_sheds_over_rate() -> None:
    clk = ManualClock()
    limiter = TokenBucketRateLimiter(rate=1.0, burst=2.0, clock=clk)
    server = _server(clk, limiter.interceptor())
    ctx = RequestContext.root(clock=clk, tenant="t1")
    headers = ctx.to_headers(clock=clk)
    # Burst of 2 allowed, the 3rd is shed.
    assert (await server.handlers()["go"](_req(headers))).ok
    assert (await server.handlers()["go"](_req(headers))).ok
    third = await server.handlers()["go"](_req(headers))
    assert third.status is RpcStatus.RESOURCE_EXHAUSTED
    # After 1s, one token refills.
    clk.advance(1.0)
    assert (await server.handlers()["go"](_req(headers))).ok


async def test_rate_limiter_is_per_tenant() -> None:
    clk = ManualClock()
    limiter = TokenBucketRateLimiter(rate=0.0, burst=1.0, clock=clk)
    assert limiter.allow(RequestContext.root(clock=clk, tenant="a"))
    # tenant a now empty, but tenant b has its own bucket.
    assert not limiter.allow(RequestContext.root(clock=clk, tenant="a"))
    assert limiter.allow(RequestContext.root(clock=clk, tenant="b"))


async def test_concurrency_limiter_caps_in_flight() -> None:
    clk = ManualClock()
    limiter = ConcurrencyLimiter(max_in_flight=0)  # nothing allowed
    server = _server(clk, limiter.interceptor())
    resp = await server.handlers()["go"](_req())
    assert resp.status is RpcStatus.RESOURCE_EXHAUSTED
    assert limiter.rejected == 1


async def test_recent_call_log_records() -> None:
    clk = ManualClock()
    audit = RecentCallLog(capacity=2)
    server = _server(clk, audit.interceptor())
    ctx = RequestContext.root(clock=clk).with_auth(AuthContext(principal="u1"))
    headers = ctx.to_headers(clock=clk)
    for _ in range(3):
        await server.handlers()["go"](_req(headers))
    recent = audit.recent()
    assert len(recent) == 2  # bounded
    assert recent[-1] == ("svc.go", "OK", "u1")


async def test_access_log_interceptor_passes_through() -> None:
    clk = ManualClock()
    server = _server(clk, access_log_interceptor())
    resp = await server.handlers()["go"](_req())
    assert resp.ok  # logging never changes the result
