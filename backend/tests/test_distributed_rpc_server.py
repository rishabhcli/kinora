"""Tests for the ServiceServer dispatch pipeline (decode/encode/dedup/authz)."""

from __future__ import annotations

from dataclasses import dataclass

from app.distributed.rpc.context import RequestContext
from app.distributed.rpc.contracts import ServiceContract, method
from app.distributed.rpc.deadline import ManualClock
from app.distributed.rpc.errors import RpcStatus, not_found
from app.distributed.rpc.messages import RpcRequest
from app.distributed.rpc.server import ServiceServer, authz_interceptor


@dataclass
class Req:
    key: str


@dataclass
class Resp:
    value: str


class Impl:
    def __init__(self) -> None:
        self.calls = 0

    async def get(self, req: Req) -> Resp:
        self.calls += 1
        if req.key == "missing":
            raise not_found("no such key")
        return Resp(value=f"v:{req.key}")

    async def crash(self, _req: Req) -> Resp:
        raise RuntimeError("kaboom")

    async def ping(self) -> Resp:
        return Resp(value="pong")


def _contract() -> ServiceContract:
    return ServiceContract.define(
        "kv",
        methods=[
            method("get", Req, Resp, idempotent=True),
            method("crash", Req, Resp),
            method("ping", None, Resp),
        ],
    )


def _request(method_name: str, payload: dict[str, object], **headers: str) -> RpcRequest:
    return RpcRequest("kv", method_name, payload=payload, headers=headers)


async def test_dispatch_decodes_and_encodes() -> None:
    clk = ManualClock()
    server = ServiceServer(contract=_contract(), impl=Impl(), clock=clk)
    handlers = server.handlers()
    resp = await handlers["get"](_request("get", {"key": "abc"}))
    assert resp.ok
    assert resp.body == {"value": "v:abc"}
    assert resp.trailers["served_by"] == "kv"


async def test_application_error_passes_through() -> None:
    clk = ManualClock()
    server = ServiceServer(contract=_contract(), impl=Impl(), clock=clk)
    resp = await server.handlers()["get"](_request("get", {"key": "missing"}))
    assert resp.status is RpcStatus.NOT_FOUND


async def test_unhandled_exception_becomes_internal() -> None:
    clk = ManualClock()
    server = ServiceServer(contract=_contract(), impl=Impl(), clock=clk)
    resp = await server.handlers()["crash"](_request("crash", {"key": "x"}))
    assert resp.status is RpcStatus.INTERNAL
    # No stack trace leaks across the seam — just a message.
    assert "kaboom" in (resp.error_message or "")


async def test_no_argument_method() -> None:
    clk = ManualClock()
    server = ServiceServer(contract=_contract(), impl=Impl(), clock=clk)
    resp = await server.handlers()["ping"](_request("ping", {}))
    assert resp.body == {"value": "pong"}


async def test_bad_payload_is_invalid_argument() -> None:
    clk = ManualClock()
    server = ServiceServer(contract=_contract(), impl=Impl(), clock=clk)
    resp = await server.handlers()["get"](_request("get", {"bogus": 1}))
    assert resp.status is RpcStatus.INVALID_ARGUMENT


async def test_deadline_already_expired_rejected() -> None:
    clk = ManualClock()
    server = ServiceServer(contract=_contract(), impl=Impl(), clock=clk)
    impl_obj = server.impl
    # A header with 0ms remaining → server rejects before running the impl.
    resp = await server.handlers()["get"](
        _request("get", {"key": "x"}, **{"x-kinora-deadline-ms": "0"})
    )
    assert resp.status is RpcStatus.DEADLINE_EXCEEDED
    assert impl_obj.calls == 0  # impl never ran


async def test_idempotent_replay_returns_cached() -> None:
    clk = ManualClock()
    impl = Impl()
    server = ServiceServer(contract=_contract(), impl=impl, clock=clk)
    handler = server.handlers()["get"]
    headers = {"x-kinora-idempotency-key": "shot#abc"}
    r1 = await handler(_request("get", {"key": "k"}, **headers))
    r2 = await handler(_request("get", {"key": "k"}, **headers))
    assert r1.body == r2.body
    assert impl.calls == 1  # second call served from the dedup cache
    assert r2.trailers.get("idempotent_replay") == "true"


async def test_non_idempotent_not_deduped() -> None:
    clk = ManualClock()

    @dataclass
    class CountResp:
        n: int

    counter = {"n": 0}

    class Counting:
        async def bump(self, _req: Req) -> CountResp:
            counter["n"] += 1
            return CountResp(n=counter["n"])

    contract = ServiceContract.define(
        "ctr", methods=[method("bump", Req, CountResp, idempotent=False)]
    )
    server = ServiceServer(contract=contract, impl=Counting(), clock=clk)
    handler = server.handlers()["bump"]
    headers = {"x-kinora-idempotency-key": "k"}
    r1 = await handler(RpcRequest("ctr", "bump", payload={"key": "x"}, headers=headers))
    r2 = await handler(RpcRequest("ctr", "bump", payload={"key": "x"}, headers=headers))
    # Not idempotent → no dedup; both ran and the counter advanced.
    assert r1.body == {"n": 1}
    assert r2.body == {"n": 2}


async def test_authz_interceptor_denies_missing_scope() -> None:
    clk = ManualClock()
    server = ServiceServer(
        contract=_contract(),
        impl=Impl(),
        clock=clk,
        interceptors=[authz_interceptor("render")],
    )
    # No scope in the context headers → PERMISSION_DENIED.
    resp = await server.handlers()["get"](_request("get", {"key": "x"}))
    assert resp.status is RpcStatus.PERMISSION_DENIED


async def test_context_rehydrated_for_impl() -> None:
    clk = ManualClock()
    captured: dict[str, object] = {}

    class CtxImpl:
        async def get(self, _req: Req) -> Resp:
            from app.distributed.rpc.context import current_context

            ctx = current_context()
            captured["trace"] = ctx.trace_id if ctx else None
            captured["tenant"] = ctx.tenant if ctx else None
            return Resp(value="ok")

    server = ServiceServer(contract=_contract(), impl=CtxImpl(), clock=clk)
    root = RequestContext.root(clock=clk, tenant="ws-9")
    headers = root.to_headers(clock=clk)
    await server.handlers()["get"](RpcRequest("kv", "get", payload={"key": "k"}, headers=headers))
    assert captured["trace"] == root.trace_id
    assert captured["tenant"] == "ws-9"
