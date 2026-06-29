"""Tests for the typed client stub + stub-source generation."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.distributed.rpc.contracts import ServiceContract, method
from app.distributed.rpc.mesh import build_test_mesh
from app.distributed.rpc.stub import ServiceStub, generate_stub_source


@dataclass
class Req:
    x: int


@dataclass
class Resp:
    y: int


def _contract() -> ServiceContract:
    return ServiceContract.define(
        "calc",
        version=2,
        methods=[method("double", Req, Resp, idempotent=True)],
    )


class Calc:
    async def double(self, req: Req) -> Resp:
        return Resp(y=req.x * 2)


async def test_stub_dynamic_method_call() -> None:
    mesh = build_test_mesh()
    mesh.register(_contract(), Calc())
    stub = mesh.stub("calc")
    assert isinstance(stub, ServiceStub)
    res = await stub.double(Req(x=21), context=mesh.new_context())
    assert res.y == 42


async def test_stub_invoke_explicit_form() -> None:
    mesh = build_test_mesh()
    mesh.register(_contract(), Calc())
    res = await mesh.stub("calc").invoke("double", Req(x=5), context=mesh.new_context())
    assert res.y == 10


def test_stub_unknown_method_attribute_error() -> None:
    mesh = build_test_mesh()
    mesh.register(_contract(), Calc())
    stub = mesh.stub("calc")
    with pytest.raises(AttributeError):
        _ = stub.nonexistent


def test_generate_stub_source_has_signatures() -> None:
    src = generate_stub_source(_contract())
    assert "class CalcStub:" in src
    assert "async def double(self, request: Req, *," in src
    assert "-> Resp" in src
    # It must be syntactically valid Python.
    compile(src, "<stub>", "exec")


def test_generate_stub_source_empty_contract() -> None:
    c = ServiceContract.define("empty", methods=[])
    src = generate_stub_source(c)
    assert "class EmptyStub:" in src
    compile(src, "<stub>", "exec")
