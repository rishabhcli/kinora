"""Tests for the transport fabric (transport.py)."""

from __future__ import annotations

import random

from app.distributed.replication.clock import NodeId
from app.distributed.replication.transport import (
    DirectTransport,
    FabricConfig,
    InMemoryFabric,
    Partition,
    messages_for,
)

US = NodeId("us", "1")
EU = NodeId("eu", "1")
AP = NodeId("ap", "1")


def test_direct_transport_delivers_in_send_order() -> None:
    t = DirectTransport()
    t.send(US, EU, "a")
    t.send(US, EU, "b")
    drained = t.drain()
    assert [m.payload for m in drained] == ["a", "b"]
    assert t.drain() == []  # drained once


def test_fabric_respects_latency() -> None:
    fab = InMemoryFabric(FabricConfig(latency_ms=10))
    fab.send_at(US, EU, "msg", now_ms=0)
    assert fab.deliver_due(5) == []  # not arrived yet
    due = fab.deliver_due(10)
    assert [m.payload for m in due] == ["msg"]


def test_fabric_per_link_latency_override() -> None:
    fab = InMemoryFabric(
        FabricConfig(latency_ms=5, link_latency_ms={("us", "ap"): 100})
    )
    fab.send_at(US, EU, "near", now_ms=0)
    fab.send_at(US, AP, "far", now_ms=0)
    near = fab.deliver_due(5)
    assert [m.payload for m in near] == ["near"]
    assert fab.deliver_due(50) == []  # far still in flight
    far = fab.deliver_due(100)
    assert [m.payload for m in far] == ["far"]


def test_partition_holds_then_heals() -> None:
    part = Partition()
    fab = InMemoryFabric(FabricConfig(latency_ms=10), partition=part)
    part.sever("us", "eu")
    fab.send_at(US, EU, "blocked", now_ms=0)
    assert fab.deliver_due(10) == []  # held by partition
    assert fab.pending() == 1
    part.heal("us", "eu")
    due = fab.deliver_due(20)
    assert [m.payload for m in due] == ["blocked"]


def test_partition_is_symmetric_by_default() -> None:
    part = Partition()
    part.sever("us", "eu")
    assert part.is_blocked(US, EU)
    assert part.is_blocked(EU, US)
    part.heal("us", "eu")
    assert not part.is_blocked(US, EU)


def test_partition_asymmetric() -> None:
    part = Partition()
    part.sever("us", "eu", symmetric=False)
    assert part.is_blocked(US, EU)
    assert not part.is_blocked(EU, US)


def test_drop_rate_is_deterministic_for_seed() -> None:
    cfg = FabricConfig(latency_ms=1, drop_rate=0.5)
    fab1 = InMemoryFabric(cfg, rng=random.Random(42))
    fab2 = InMemoryFabric(cfg, rng=random.Random(42))
    delivered1, delivered2 = 0, 0
    for i in range(100):
        fab1.send_at(US, EU, i, now_ms=0)
        fab2.send_at(US, EU, i, now_ms=0)
    delivered1 = len(fab1.deliver_due(10))
    delivered2 = len(fab2.deliver_due(10))
    assert delivered1 == delivered2  # same seed -> same drops
    assert 0 < delivered1 < 100  # some dropped, some delivered


def test_messages_for_filters_by_destination() -> None:
    fab = InMemoryFabric(FabricConfig(latency_ms=1))
    fab.send_at(US, EU, "to-eu", now_ms=0)
    fab.send_at(US, AP, "to-ap", now_ms=0)
    due = fab.deliver_due(5)
    assert [m.payload for m in messages_for(due, EU)] == ["to-eu"]
    assert [m.payload for m in messages_for(due, AP)] == ["to-ap"]


def test_heal_all_clears_every_partition() -> None:
    part = Partition()
    part.sever("us", "eu")
    part.sever("us", "ap")
    part.heal_all()
    assert part.severed_pairs == frozenset()
