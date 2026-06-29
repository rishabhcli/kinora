"""Tests for the service registry + discovery seam."""

from __future__ import annotations

import pytest

from app.distributed.rpc.contracts import ServiceContract, method
from app.distributed.rpc.deadline import ManualClock
from app.distributed.rpc.registry import (
    Discovery,
    InstanceHealth,
    ServiceRegistry,
)


def _contract(name: str = "memory", version: int = 1) -> ServiceContract:
    return ServiceContract.define(name, version=version, methods=[method("read", None, None)])


def test_register_instance_and_resolve() -> None:
    clk = ManualClock()
    reg = ServiceRegistry(clock=clk)
    reg.register_instance("memory", "m0")
    disc = Discovery(registry=reg)
    found = disc.resolve("memory")
    assert len(found) == 1
    assert found[0].instance_id == "m0"
    assert found[0].health


def test_contract_conflict_rejected() -> None:
    reg = ServiceRegistry()
    reg.register_contract(_contract())
    reg.register_contract(_contract())  # identical fingerprint → idempotent ok
    bad = ServiceContract.define(
        "memory", methods=[method("read", None, None), method("write", None, None)]
    )
    with pytest.raises(ValueError):
        reg.register_contract(bad)


def test_heartbeat_updates_health() -> None:
    clk = ManualClock()
    reg = ServiceRegistry(clock=clk)
    reg.register_instance("memory", "m0")
    reg.heartbeat("memory", "m0", healthy=False, status=InstanceHealth.UNHEALTHY)
    disc = Discovery(registry=reg)
    assert disc.resolve("memory") == []  # unhealthy filtered out
    all_inst = disc.resolve("memory", include_unhealthy=True)
    assert all_inst[0].health_status is InstanceHealth.UNHEALTHY


def test_ttl_marks_stale_unhealthy() -> None:
    clk = ManualClock()
    reg = ServiceRegistry(clock=clk)
    reg.register_instance("memory", "m0")  # heartbeat at t=0
    disc = Discovery(registry=reg, ttl_s=10.0)
    assert len(disc.resolve("memory")) == 1
    clk.advance(11.0)  # heartbeat now stale
    assert disc.resolve("memory") == []
    stale = disc.resolve("memory", include_unhealthy=True)
    assert stale[0].health is False


def test_min_version_filter() -> None:
    clk = ManualClock()
    reg = ServiceRegistry(clock=clk)
    reg.register_instance("memory", "v1", version=1)
    reg.register_instance("memory", "v2", version=2)
    disc = Discovery(registry=reg)
    assert {i.instance_id for i in disc.resolve("memory", min_version=2)} == {"v2"}
    assert {i.instance_id for i in disc.resolve("memory", min_version=1)} == {"v1", "v2"}


def test_deregister_removes() -> None:
    clk = ManualClock()
    reg = ServiceRegistry(clock=clk)
    reg.register_instance("memory", "m0")
    reg.deregister("memory", "m0")
    disc = Discovery(registry=reg)
    assert not disc.has_service("memory")
    assert disc.services() == []


def test_multiple_instances_resolve() -> None:
    clk = ManualClock()
    reg = ServiceRegistry(clock=clk)
    for i in range(3):
        reg.register_instance("gen", f"g{i}")
    disc = Discovery(registry=reg)
    assert len(disc.resolve("gen")) == 3
    assert disc.services() == ["gen"]
