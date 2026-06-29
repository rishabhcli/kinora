"""Tests for the resilient RPC client (policy composition end-to-end)."""

from __future__ import annotations

from app.distributed.rpc.circuit import CircuitConfig
from app.distributed.rpc.client import (
    CallOptions,
    RpcClient,
    constant_transport_resolver,
)
from app.distributed.rpc.context import RequestContext
from app.distributed.rpc.deadline import ManualClock
from app.distributed.rpc.errors import FailureKind, RpcStatus
from app.distributed.rpc.hedging import HedgeBudget, HedgePolicy
from app.distributed.rpc.loadbalancer import LoadBalancePolicy
from app.distributed.rpc.messages import RpcRequest, RpcResponse
from app.distributed.rpc.registry import Discovery, ServiceRegistry
from app.distributed.rpc.retry import BackoffPolicy, RetryPolicy
from app.distributed.rpc.transport import DeterministicFakeTransport


def _make_client(
    transport: DeterministicFakeTransport,
    *,
    clock: ManualClock,
    retry: RetryPolicy | None = None,
    instances: int = 1,
) -> RpcClient:
    reg = ServiceRegistry(clock=clock)
    for i in range(instances):
        reg.register_instance("svc", f"svc-{i}")
    disc = Discovery(registry=reg)

    async def _sleep(s: float) -> None:
        if s > 0:
            clock.advance(s)

    return RpcClient(
        discovery=disc,
        transport_resolver=constant_transport_resolver(transport),
        clock=clock,
        sleep=_sleep,
        default_retry=retry or RetryPolicy(backoff=BackoffPolicy(jitter="none")),
        default_lb_policy=LoadBalancePolicy.ROUND_ROBIN,
    )


def _ctx(clock: ManualClock, **kw: object) -> RequestContext:
    return RequestContext.root(clock=clock, timeout_s=5.0, **kw)  # type: ignore[arg-type]


async def test_successful_call_returns_body() -> None:
    clk = ManualClock()
    t = DeterministicFakeTransport(default_body={"ok": 1})
    client = _make_client(t, clock=clk)
    resp = await client.call("svc", "m", {"a": 1}, context=_ctx(clk))
    assert resp.ok
    assert resp.body == {"ok": 1}
    assert len(t.sends) == 1


async def test_propagates_context_headers() -> None:
    clk = ManualClock()
    t = DeterministicFakeTransport(default_body=None)
    client = _make_client(t, clock=clk)
    ctx = _ctx(clk, principal="reader-7", tenant="ws-3")
    await client.call("svc", "m", {}, context=ctx)
    sent = t.sends[0]
    assert sent.headers["x-kinora-trace-id"] == ctx.trace_id
    assert sent.headers["x-kinora-principal"] == "reader-7"
    assert sent.headers["x-kinora-tenant"] == "ws-3"
    # The hop opened a child span whose parent is the originating span.
    assert sent.headers["x-kinora-parent-span-id"] == ctx.span_id


async def test_retries_transient_then_succeeds() -> None:
    clk = ManualClock()
    state = {"n": 0}

    def flaky(_req: RpcRequest) -> RpcResponse:
        state["n"] += 1
        if state["n"] < 3:
            return RpcResponse.failure(RpcStatus.UNAVAILABLE, "down", kind=FailureKind.TRANSPORT)
        return RpcResponse.success({"ok": True})

    t = DeterministicFakeTransport(responders={"svc.m": flaky})
    client = _make_client(t, clock=clk, retry=RetryPolicy(
        max_attempts=5, backoff=BackoffPolicy(base_delay_s=0.01, jitter="none")
    ))
    # Idempotent method → retried.
    resp = await client.call(
        "svc", "m", {}, context=_ctx(clk), options=CallOptions(idempotent=True)
    )
    assert resp.ok
    assert state["n"] == 3


async def test_non_idempotent_not_retried() -> None:
    clk = ManualClock()
    state = {"n": 0}

    def always_fail(_req: RpcRequest) -> RpcResponse:
        state["n"] += 1
        return RpcResponse.failure(RpcStatus.UNAVAILABLE, "down", kind=FailureKind.TRANSPORT)

    t = DeterministicFakeTransport(responders={"svc.m": always_fail})
    client = _make_client(t, clock=clk, retry=RetryPolicy(max_attempts=5))
    resp = await client.call(
        "svc", "m", {}, context=_ctx(clk), options=CallOptions(idempotent=False)
    )
    assert not resp.ok
    assert state["n"] == 1


async def test_no_endpoint_is_unavailable() -> None:
    clk = ManualClock()
    reg = ServiceRegistry(clock=clk)  # no instances registered
    disc = Discovery(registry=reg)
    t = DeterministicFakeTransport()
    client = RpcClient(
        discovery=disc,
        transport_resolver=constant_transport_resolver(t),
        clock=clk,
    )
    resp = await client.call("ghost", "m", {}, context=_ctx(clk))
    assert resp.status is RpcStatus.UNAVAILABLE
    assert len(t.sends) == 0  # never sent — discovery had nothing


async def test_circuit_opens_and_fast_fails() -> None:
    clk = ManualClock()

    def always_fail(_req: RpcRequest) -> RpcResponse:
        return RpcResponse.failure(RpcStatus.UNAVAILABLE, "down", kind=FailureKind.TRANSPORT)

    t = DeterministicFakeTransport(responders={"svc.m": always_fail})
    client = _make_client(t, clock=clk, retry=RetryPolicy(max_attempts=1))
    client.breakers.default_config = CircuitConfig(
        failure_threshold=0.5, min_samples=3, reset_timeout_s=10.0
    )
    # Drive enough failures to open the breaker.
    for _ in range(5):
        await client.call("svc", "m", {}, context=_ctx(clk))
    sends_before = len(t.sends)
    # Next call should fast-fail without touching the transport.
    resp = await client.call("svc", "m", {}, context=_ctx(clk))
    assert resp.status is RpcStatus.UNAVAILABLE
    assert len(t.sends) == sends_before  # breaker short-circuited


async def test_deadline_exhausted_is_deadline_exceeded() -> None:
    clk = ManualClock()
    t = DeterministicFakeTransport(default_body=None)
    client = _make_client(t, clock=clk)
    # An already-expired inherited deadline.
    ctx = RequestContext.root(clock=clk, timeout_s=0.0)
    resp = await client.call("svc", "m", {}, context=ctx)
    assert resp.status is RpcStatus.DEADLINE_EXCEEDED


async def test_max_depth_guard() -> None:
    clk = ManualClock()
    t = DeterministicFakeTransport(default_body=None)
    client = _make_client(t, clock=clk)
    client.max_depth = 2
    ctx = _ctx(clk)
    deep = ctx.child().child().child()  # depth 3 > max 2
    resp = await client.call("svc", "m", {}, context=deep)
    assert resp.status is RpcStatus.RESOURCE_EXHAUSTED


async def test_hedging_races_to_second_instance() -> None:
    clk = ManualClock()

    # Primary instance is slow-fail; the hedge to another instance succeeds.
    def responder(req: RpcRequest) -> RpcResponse:
        # attempt 0 = primary (fail), attempt 1 = hedge (succeed)
        if req.attempt == 0:
            return RpcResponse.failure(RpcStatus.UNAVAILABLE, "slow", kind=FailureKind.TRANSPORT)
        return RpcResponse.success({"hedge": True})

    t = DeterministicFakeTransport(responders={"svc.m": responder})
    client = _make_client(t, clock=clk, retry=RetryPolicy(max_attempts=1), instances=2)
    resp = await client.call(
        "svc",
        "m",
        {},
        context=_ctx(clk),
        options=CallOptions(
            idempotent=True,
            hedge=HedgePolicy(
                delay_s=0.01, max_hedges=1, budget=HedgeBudget(ratio=2.0)
            ),
        ),
    )
    # The hedge leg (attempt 1) won.
    assert resp.ok
    assert resp.body == {"hedge": True}
