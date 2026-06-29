"""Tests for anti-entropy reconciliation (antientropy.py)."""

from __future__ import annotations

from app.distributed.replication.antientropy import Reconciler
from app.distributed.replication.clock import (
    HybridLogicalClock,
    ManualClock,
    NodeId,
)
from app.distributed.replication.conflict import LWWResolver, ResolverRegistry
from app.distributed.replication.node import ReplicaNode

US = NodeId("us", "1")
EU = NodeId("eu", "1")


def make_node(node: NodeId, start: int = 0) -> ReplicaNode:
    return ReplicaNode(
        node, HybridLogicalClock(node, ManualClock(start)), ResolverRegistry(default=LWWResolver())
    )


def test_delta_sync_catches_up_a_lagging_peer() -> None:
    us = make_node(US)
    eu = make_node(EU)
    us.put("a", 1)
    us.put("b", 2)
    rec_us = Reconciler(us)
    rec_eu = Reconciler(eu)
    # eu advertises its (empty) frontier; us answers with the full delta.
    request = rec_eu.make_request()
    response = rec_us.answer(request)
    applied = rec_eu.apply_response(response)
    assert applied == 2
    assert eu.get("a") == 1
    assert eu.get("b") == 2
    assert eu.frontier() == us.frontier()


def test_delta_sync_is_idempotent() -> None:
    us = make_node(US)
    eu = make_node(EU)
    us.put("a", 1)
    rec_us, rec_eu = Reconciler(us), Reconciler(eu)
    response = rec_us.answer(rec_eu.make_request())
    rec_eu.apply_response(response)
    # applying the same response again changes nothing.
    again = rec_eu.apply_response(response)
    assert again == 0
    assert eu.get("a") == 1


def test_bidirectional_sync_converges_concurrent_writes() -> None:
    us = make_node(US, start=100)
    eu = make_node(EU, start=200)  # eu's clock ahead -> its writes win on LWW ties
    us.put("shared", "us-value")
    eu.put("shared", "eu-value")
    us.put("only-us", 1)
    eu.put("only-eu", 2)
    rec_us, rec_eu = Reconciler(us), Reconciler(eu)
    # exchange both directions
    rec_eu.apply_response(rec_us.answer(rec_eu.make_request()))
    rec_us.apply_response(rec_eu.answer(rec_us.make_request()))
    # both converge to identical state
    assert us.get("only-us") == 1
    assert us.get("only-eu") == 2
    assert eu.get("only-us") == 1
    assert eu.get("only-eu") == 2
    # LWW on the shared key: eu wrote at a later wall clock.
    assert us.get("shared") == eu.get("shared") == "eu-value"


def test_merkle_repair_recovers_a_lost_write() -> None:
    us = make_node(US, start=100)
    eu = make_node(EU, start=100)
    # us has data eu never received (simulating dropped log shipping + compaction).
    us.put("x", "recovered")
    us.put("y", "also")
    rec_us, rec_eu = Reconciler(us), Reconciler(eu)
    # eu advertises a Merkle digest; us computes the divergent buckets and ships cells.
    eu_digest = rec_eu.digest()
    repair = rec_us.repair(eu_digest)
    assert repair.is_repair
    applied = rec_eu.apply_response(repair)
    assert applied == 2
    assert eu.get("x") == "recovered"
    assert eu.get("y") == "also"


def test_merkle_repair_no_divergence_is_noop() -> None:
    us = make_node(US, start=100)
    eu = make_node(EU, start=100)
    us.put("x", 1)
    # bring eu fully in sync via delta first
    rec_us, rec_eu = Reconciler(us), Reconciler(eu)
    rec_eu.apply_response(rec_us.answer(rec_eu.make_request()))
    # now a Merkle repair should find nothing to do
    repair = rec_us.repair(rec_eu.digest())
    assert repair.is_empty
    assert rec_eu.apply_response(repair) == 0


def test_merkle_repair_respects_lww_does_not_clobber_newer() -> None:
    us = make_node(US, start=100)
    eu = make_node(EU, start=500)  # eu has the newer write
    us.put("k", "old")
    eu.put("k", "new")
    rec_us, rec_eu = Reconciler(us), Reconciler(eu)
    # us tries to repair eu with its older value -> must NOT overwrite eu's newer.
    repair = rec_us.repair(rec_eu.digest())
    rec_eu.apply_response(repair)
    assert eu.get("k") == "new"
