"""Tests for the gossip engine (gossip.py)."""

from __future__ import annotations

from app.distributed.replication.clock import (
    HybridLogicalClock,
    ManualClock,
    NodeId,
)
from app.distributed.replication.conflict import LWWResolver, ResolverRegistry
from app.distributed.replication.gossip import GossipEngine
from app.distributed.replication.node import ReplicaNode
from app.distributed.replication.transport import DirectTransport, FabricConfig, InMemoryFabric

US = NodeId("us", "1")
EU = NodeId("eu", "1")
AP = NodeId("ap", "1")


def make_node(node: NodeId, clock: ManualClock) -> ReplicaNode:
    return ReplicaNode(
        node, HybridLogicalClock(node, clock), ResolverRegistry(default=LWWResolver())
    )


def test_push_propagates_local_writes() -> None:
    transport = DirectTransport()
    us_node = make_node(US, ManualClock(0))
    eu_node = make_node(EU, ManualClock(0))
    us_eng = GossipEngine(us_node, transport, [US, EU])
    eu_eng = GossipEngine(eu_node, transport, [US, EU])

    us_node.put("k", "v")
    # us ticks -> pushes the new record onto the transport.
    report = us_eng.tick([], now_ms=0)
    assert report.pushed >= 1
    # eu ticks with the delivered messages -> applies.
    msgs = transport.drain()
    eu_eng.tick(msgs, now_ms=0)
    assert eu_node.get("k") == "v"


def test_pull_request_response_round() -> None:
    transport = DirectTransport()
    us_node = make_node(US, ManualClock(0))
    eu_node = make_node(EU, ManualClock(0))
    us_node.put("a", 1)  # us has data before engines exist
    us_eng = GossipEngine(us_node, transport, [US, EU])
    eu_eng = GossipEngine(eu_node, transport, [US, EU])

    # mark us's data as already-pushed so only the PULL path can move it.
    us_eng._last_pushed = us_node.frontier()  # noqa: SLF001 - test setup

    # eu ticks -> sends a pull request to us.
    eu_eng.tick([], now_ms=0)
    # us answers the pull request.
    us_eng.tick(transport.drain(), now_ms=0)
    # eu applies the pull response.
    eu_eng.tick(transport.drain(), now_ms=0)
    assert eu_node.get("a") == 1


def test_two_node_convergence_over_lossy_fabric() -> None:
    fabric = InMemoryFabric(FabricConfig(latency_ms=20))
    us_node = make_node(US, ManualClock(0))
    eu_node = make_node(EU, ManualClock(0))
    us_eng = GossipEngine(us_node, fabric, [US, EU])
    eu_eng = GossipEngine(eu_node, fabric, [US, EU])

    us_node.put("x", "from-us")
    eu_node.put("y", "from-eu")

    for t in range(0, 500, 20):
        due = fabric.deliver_due(t)
        us_eng.tick(due, t)
        eu_eng.tick(due, t)

    assert us_node.get("y") == "from-eu"
    assert eu_node.get("x") == "from-us"
    assert us_node.frontier() == eu_node.frontier()
