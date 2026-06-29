"""Tests for typed service contracts + the encode/decode codec + messages."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from pydantic import BaseModel

from app.distributed.rpc.contracts import (
    ServiceContract,
    decode_value,
    encode_value,
    method,
)
from app.distributed.rpc.errors import RpcError, RpcStatus
from app.distributed.rpc.messages import RpcRequest, RpcResponse

# -- DTO fixtures ----------------------------------------------------------- #


@dataclass
class PlanReq:
    shot_hash: str
    beats: int = 1


@dataclass
class PlanResp:
    spec: dict[str, object] = field(default_factory=dict)


class PydReq(BaseModel):
    name: str
    n: int = 0


class PydResp(BaseModel):
    ok: bool


# -- codec ------------------------------------------------------------------ #


def test_encode_decode_dataclass_roundtrip() -> None:
    spec = method("plan", PlanReq, PlanResp)
    payload = spec.encode_request(PlanReq(shot_hash="h1", beats=3))
    assert payload == {"shot_hash": "h1", "beats": 3}
    decoded = spec.decode_request(payload)
    assert isinstance(decoded, PlanReq)
    assert decoded.shot_hash == "h1" and decoded.beats == 3


def test_encode_decode_pydantic_roundtrip() -> None:
    spec = method("p", PydReq, PydResp)
    payload = spec.encode_request(PydReq(name="x", n=2))
    assert payload == {"name": "x", "n": 2}
    decoded = spec.decode_request(payload)
    assert isinstance(decoded, PydReq)
    assert decoded.name == "x"
    body = spec.encode_response(PydResp(ok=True))
    assert body == {"ok": True}
    resp = spec.decode_response(body)
    assert isinstance(resp, PydResp) and resp.ok is True


def test_decode_rejects_unknown_dataclass_fields() -> None:
    spec = method("plan", PlanReq, PlanResp)
    with pytest.raises(RpcError) as exc:
        spec.decode_request({"shot_hash": "h", "bogus": 1})
    assert exc.value.status is RpcStatus.INVALID_ARGUMENT


def test_decode_rejects_invalid_pydantic() -> None:
    spec = method("p", PydReq, PydResp)
    with pytest.raises(RpcError) as exc:
        spec.decode_request({"n": "not-an-int", "name": 5})
    assert exc.value.status is RpcStatus.INVALID_ARGUMENT


def test_scalar_body_is_wrapped_and_unwrapped() -> None:
    spec = method("echo", str, str)
    payload = spec.encode_request("hello")
    assert payload == {"_value": "hello"}
    assert spec.decode_request(payload) == "hello"


def test_void_method_encodes_empty() -> None:
    spec = method("ping", None, None)
    assert spec.encode_request(None) == {}
    assert spec.decode_request({}) is None
    assert spec.encode_response(None) is None


def test_encode_value_dict_passthrough() -> None:
    assert encode_value({"a": 1}, dict) == {"a": 1}
    assert decode_value({"a": 1}, dict) == {"a": 1}


# -- ServiceContract -------------------------------------------------------- #


def test_define_rejects_duplicate_methods() -> None:
    with pytest.raises(ValueError):
        ServiceContract.define(
            "svc",
            methods=[method("a", None, None), method("a", None, None)],
        )


def test_define_rejects_bad_name() -> None:
    with pytest.raises(ValueError):
        ServiceContract.define("bad name!", methods=[])


def test_qualified_name_and_method_lookup() -> None:
    c = ServiceContract.define("memory", version=2, methods=[method("read", None, None)])
    assert c.qualified_name == "memory@v2"
    assert c.has_method("read")
    assert not c.has_method("nope")
    with pytest.raises(RpcError) as exc:
        c.method("nope")
    assert exc.value.status is RpcStatus.INVALID_ARGUMENT


def test_fingerprint_is_order_independent_and_stable() -> None:
    c1 = ServiceContract.define(
        "s", methods=[method("a", PlanReq, PlanResp), method("b", None, None)]
    )
    c2 = ServiceContract.define(
        "s", methods=[method("b", None, None), method("a", PlanReq, PlanResp)]
    )
    assert c1.fingerprint() == c2.fingerprint()
    # A signature change moves the fingerprint.
    c3 = ServiceContract.define(
        "s", methods=[method("a", PlanReq, PlanResp, idempotent=True), method("b", None, None)]
    )
    assert c3.fingerprint() != c1.fingerprint()


# -- messages --------------------------------------------------------------- #


def test_response_success_and_raise_for_status() -> None:
    ok = RpcResponse.success({"x": 1}, trailers={"served_by": "s"})
    assert ok.ok
    assert ok.raise_for_status() == {"x": 1}


def test_response_failure_cannot_be_ok() -> None:
    with pytest.raises(ValueError):
        RpcResponse.failure(RpcStatus.OK, "no")


def test_response_to_error_roundtrip() -> None:
    resp = RpcResponse.from_error(RpcError(RpcStatus.NOT_FOUND, "gone"))
    assert not resp.ok
    err = resp.to_error(service="memory", method="read")
    assert err.status is RpcStatus.NOT_FOUND
    assert err.service == "memory"


def test_request_for_attempt_and_headers() -> None:
    req = RpcRequest("s", "m", payload={"a": 1}, headers={"h": "1"})
    assert req.endpoint == "s.m"
    a2 = req.for_attempt(2)
    assert a2.attempt == 2 and a2.payload == {"a": 1}
    merged = req.with_headers({"h2": "2"})
    assert merged.headers == {"h": "1", "h2": "2"}
