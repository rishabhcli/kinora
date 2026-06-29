"""Tests for the replica node and causal delivery (node.py)."""

from __future__ import annotations

import pytest

from app.distributed.replication.clock import (
    HybridLogicalClock,
    ManualClock,
    NodeId,
)
from app.distributed.replication.conflict import LWWResolver, ResolverRegistry
from app.distributed.replication.node import ReplicaNode

US = NodeId("us", "1")
EU = NodeId("eu", "1")


def make_node(node: NodeId, clock: ManualClock) -> ReplicaNode:
    return ReplicaNode(
        node, HybridLogicalClock(node, clock), ResolverRegistry(default=LWWResolver())
    )


def test_clock_node_mismatch_rejected() -> None:
    clock = ManualClock(0)
    with pytest.raises(ValueError):
        ReplicaNode(US, HybridLogicalClock(EU, clock), ResolverRegistry(default=LWWResolver()))


def test_local_write_applies_and_logs() -> None:
    node = make_node(US, ManualClock(100))
    receipt = node.put("k", "v")
    assert node.get("k") == "v"
    assert receipt.node == US
    assert node.frontier().get(US) == 1
    assert len(node.log) == 1


def test_local_writes_increment_sequence() -> None:
    node = make_node(US, ManualClock(100))
    r1 = node.put("a", 1)
    r2 = node.put("b", 2)
    assert r1.record.seq == 1
    assert r2.record.seq == 2
    assert r2.record.deps.get(US) == 1  # depends on the prior write


def test_ingest_applies_ready_record() -> None:
    src = make_node(US, ManualClock(100))
    dst = make_node(EU, ManualClock(100))
    receipt = src.put("k", "from-us")
    result = dst.ingest(receipt.record)
    assert result.accepted
    assert result.applied == 1
    assert not result.buffered
    assert dst.get("k") == "from-us"


def test_ingest_is_idempotent() -> None:
    src = make_node(US, ManualClock(100))
    dst = make_node(EU, ManualClock(100))
    rec = src.put("k", "v").record
    dst.ingest(rec)
    again = dst.ingest(rec)
    assert not again.accepted


def test_out_of_order_record_is_buffered_then_delivered() -> None:
    src = make_node(US, ManualClock(100))
    dst = make_node(EU, ManualClock(100))
    r1 = src.put("a", 1).record  # seq 1
    r2 = src.put("b", 2).record  # seq 2, depends on seq 1
    # deliver seq 2 first -> must buffer (gap at seq 1).
    res2 = dst.ingest(r2)
    assert res2.buffered
    assert res2.applied == 0
    assert dst.get("b") is None
    assert dst.buffered_count() == 1
    # now seq 1 arrives -> both apply.
    res1 = dst.ingest(r1)
    assert res1.applied == 2
    assert dst.buffered_count() == 0
    assert dst.get("a") == 1
    assert dst.get("b") == 2


def test_cross_node_causal_dependency_buffers() -> None:
    """A write on EU that depends on a US write must wait for that US write."""
    us = make_node(US, ManualClock(100))
    eu = make_node(EU, ManualClock(100))
    third = make_node(NodeId("ap", "1"), ManualClock(100))

    us_rec = us.put("x", "us").record
    eu.ingest(us_rec)  # eu now has x
    eu_rec = eu.put("y", "eu").record  # depends on having seen us_rec

    # third sees eu_rec first; it depends on us_rec which third lacks -> buffer.
    res = third.ingest(eu_rec)
    assert res.buffered
    assert third.get("y") is None
    # deliver the dependency -> eu_rec unblocks.
    third.ingest(us_rec)
    assert third.get("y") == "eu"
    assert third.get("x") == "us"


def test_delta_since_feeds_a_catchup() -> None:
    src = make_node(US, ManualClock(100))
    dst = make_node(EU, ManualClock(100))
    src.put("a", 1)
    src.put("b", 2)
    src.put("c", 3)
    delta = src.delta_since(dst.frontier())
    applied = dst.ingest_many(delta)
    assert applied == 3
    assert dst.get("c") == 3
    assert dst.frontier() == src.frontier()
