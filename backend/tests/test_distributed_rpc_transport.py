"""Tests for the transport seam: in-process, loopback, and the fake."""

from __future__ import annotations

import pytest

from app.distributed.rpc.errors import FailureKind, RpcError, RpcStatus
from app.distributed.rpc.messages import RpcRequest, RpcResponse
from app.distributed.rpc.transport import (
    DeterministicFakeTransport,
    InProcessTransport,
    LoopbackTransport,
)


async def _echo(request: RpcRequest) -> RpcResponse:
    return RpcResponse.success({"echo": request.payload, "attempt": request.attempt})


# -- InProcessTransport ----------------------------------------------------- #


async def test_inprocess_delivers_to_handler() -> None:
    t = InProcessTransport()
    t.bind("svc", "m", _echo)
    resp = await t.send(RpcRequest("svc", "m", payload={"a": 1}))
    assert resp.ok
    assert resp.body == {"echo": {"a": 1}, "attempt": 0}


async def test_inprocess_unknown_endpoint_is_transport_unavailable() -> None:
    t = InProcessTransport()
    with pytest.raises(RpcError) as exc:
        await t.send(RpcRequest("svc", "missing"))
    assert exc.value.status is RpcStatus.UNAVAILABLE
    assert exc.value.is_transport


async def test_inprocess_bind_and_unbind_service() -> None:
    t = InProcessTransport()
    t.bind_service("svc", {"a": _echo, "b": _echo})
    assert t.endpoints() == ["svc.a", "svc.b"]
    t.unbind_service("svc")
    assert t.endpoints() == []


async def test_inprocess_closed_fails() -> None:
    t = InProcessTransport()
    t.bind("svc", "m", _echo)
    await t.aclose()
    with pytest.raises(RpcError):
        await t.send(RpcRequest("svc", "m"))


# -- LoopbackTransport ------------------------------------------------------ #


async def test_loopback_roundtrips_through_json() -> None:
    t = LoopbackTransport()
    t.bind("svc", "m", _echo)
    resp = await t.send(RpcRequest("svc", "m", payload={"nested": {"x": [1, 2, 3]}}))
    assert resp.ok
    assert resp.body == {"echo": {"nested": {"x": [1, 2, 3]}}, "attempt": 0}


async def test_loopback_rejects_non_serializable_payload() -> None:
    t = LoopbackTransport()
    t.bind("svc", "m", _echo)
    # A set is not JSON serializable — modelling a not-yet-split-ready payload.
    bad_payload: dict[str, object] = {"bad": {1, 2}}
    with pytest.raises(RpcError) as exc:
        await t.send(RpcRequest("svc", "m", payload=bad_payload))
    assert exc.value.is_transport
    assert exc.value.status is RpcStatus.INTERNAL


# -- DeterministicFakeTransport --------------------------------------------- #


async def test_fake_records_calls_and_returns_default() -> None:
    t = DeterministicFakeTransport(default_body={"ok": True})
    resp = await t.send(RpcRequest("svc", "m", payload={"a": 1}))
    assert resp.ok and resp.body == {"ok": True}
    assert len(t.sends) == 1
    assert t.sends_to("svc.m")[0].payload == {"a": 1}


async def test_fake_fault_rate_is_deterministic() -> None:
    t = DeterministicFakeTransport(fault_rate=1.0, fault_status=RpcStatus.UNAVAILABLE, seed=7)
    with pytest.raises(RpcError) as exc:
        await t.send(RpcRequest("svc", "m"))
    assert exc.value.kind is FailureKind.TRANSPORT
    assert exc.value.status is RpcStatus.UNAVAILABLE


async def test_fake_responder_overrides() -> None:
    def boom(_req: RpcRequest) -> RpcResponse:
        return RpcResponse.failure(RpcStatus.NOT_FOUND, "nope")

    t = DeterministicFakeTransport(responders={"svc.m": boom})
    resp = await t.send(RpcRequest("svc", "m"))
    assert resp.status is RpcStatus.NOT_FOUND


async def test_fake_responder_can_raise_transport_error() -> None:
    def refuse(_req: RpcRequest) -> RpcError:
        return RpcError(RpcStatus.UNAVAILABLE, "refused", kind=FailureKind.TRANSPORT)

    t = DeterministicFakeTransport(responders={"svc.m": refuse})
    with pytest.raises(RpcError):
        await t.send(RpcRequest("svc", "m"))


async def test_fake_wraps_inner_transport() -> None:
    inner = InProcessTransport()
    inner.bind("svc", "m", _echo)
    t = DeterministicFakeTransport(inner=inner)
    resp = await t.send(RpcRequest("svc", "m", payload={"z": 9}))
    assert resp.body == {"echo": {"z": 9}, "attempt": 0}
