"""Convergence proofs via the multi-region simulator (simulator.py)."""

from __future__ import annotations

import pytest

from app.distributed.replication.clock import NodeId
from app.distributed.replication.conflict import (
    GCounterResolver,
    GCounterValue,
    ResolverRegistry,
)
from app.distributed.replication.simulator import (
    MultiRegionSimulator,
    PartitionEvent,
    Scenario,
    WriteEvent,
    assert_converged,
)
from app.distributed.replication.transport import FabricConfig

US = NodeId("us", "1")
EU = NodeId("eu", "1")
AP = NodeId("ap", "1")
THREE = [US, EU, AP]


def test_simple_three_region_propagation_converges() -> None:
    scenario = Scenario(
        nodes=THREE,
        writes=[
            WriteEvent(0, US, "a", "av"),
            WriteEvent(0, EU, "b", "bv"),
            WriteEvent(0, AP, "c", "cv"),
        ],
        settle_ms=2000,
        fabric=FabricConfig(latency_ms=30),
    )
    report = MultiRegionSimulator(scenario).run()
    assert_converged(report)
    for node_id in THREE:
        state = report.states[node_id]
        assert state == {"a": "av", "b": "bv", "c": "cv"}


def test_concurrent_conflicting_writes_resolve_by_lww() -> None:
    # Space writes into distinct ticks (tick_ms=50) so wall clocks differ and
    # LWW orders by real time; EU writes last -> EU wins on every replica.
    scenario = Scenario(
        nodes=THREE,
        writes=[
            WriteEvent(0, US, "k", "us"),
            WriteEvent(100, AP, "k", "ap"),
            WriteEvent(200, EU, "k", "eu"),  # latest wall clock -> wins
        ],
        settle_ms=3000,
        fabric=FabricConfig(latency_ms=40),
    )
    report = MultiRegionSimulator(scenario).run()
    assert_converged(report)
    values = {report.states[n]["k"] for n in THREE}
    assert len(values) == 1  # all agree
    assert values == {"eu"}  # the latest write wins


def test_same_tick_concurrent_writes_resolve_deterministically() -> None:
    # All three writes land in the SAME tick (identical wall clock); LWW must
    # still pick one winner deterministically via the node tiebreak, and every
    # replica must agree on it.
    scenario = Scenario(
        nodes=THREE,
        writes=[
            WriteEvent(0, US, "k", "us"),
            WriteEvent(0, EU, "k", "eu"),
            WriteEvent(0, AP, "k", "ap"),
        ],
        settle_ms=3000,
        fabric=FabricConfig(latency_ms=40),
    )
    report = MultiRegionSimulator(scenario).run()
    assert_converged(report)
    values = {report.states[n]["k"] for n in THREE}
    assert len(values) == 1  # convergence: all agree on one winner


def test_partition_then_heal_converges() -> None:
    """US is cut off, both sides write, then the partition heals -> reconverge."""
    scenario = Scenario(
        nodes=THREE,
        partitions=[
            PartitionEvent(100, "us", "eu", up=False),
            PartitionEvent(100, "us", "ap", up=False),
            PartitionEvent(2000, "us", "eu", up=True),
            PartitionEvent(2000, "us", "ap", up=True),
        ],
        writes=[
            WriteEvent(500, US, "during-partition-us", "x"),
            WriteEvent(500, EU, "during-partition-eu", "y"),
            WriteEvent(700, US, "shared", "us-wrote"),
            WriteEvent(800, EU, "shared", "eu-wrote"),  # later -> wins
        ],
        settle_ms=5000,
        fabric=FabricConfig(latency_ms=30),
        heartbeat_timeout_ms=300,
    )
    report = MultiRegionSimulator(scenario).run()
    assert_converged(report)
    # everyone has both partition-era writes after healing.
    for node_id in THREE:
        state = report.states[node_id]
        assert state["during-partition-us"] == "x"
        assert state["during-partition-eu"] == "y"
        assert state["shared"] == "eu-wrote"


def test_clock_skew_does_not_break_convergence() -> None:
    scenario = Scenario(
        nodes=THREE,
        clock_skew_ms={EU: 200, AP: -150},  # EU runs ahead, AP behind
        writes=[
            WriteEvent(0, US, "k", "us"),
            WriteEvent(0, EU, "k", "eu"),
            WriteEvent(0, AP, "k", "ap"),
        ],
        settle_ms=3000,
        fabric=FabricConfig(latency_ms=25),
    )
    report = MultiRegionSimulator(scenario).run()
    assert_converged(report)
    # all converge to a single value even with skewed clocks.
    assert len({report.states[n]["k"] for n in THREE}) == 1


def test_deletes_propagate_and_converge() -> None:
    scenario = Scenario(
        nodes=THREE,
        writes=[
            WriteEvent(0, US, "k", "v"),
            WriteEvent(500, EU, "k", None, delete=True),  # later delete wins
        ],
        settle_ms=3000,
        fabric=FabricConfig(latency_ms=30),
    )
    report = MultiRegionSimulator(scenario).run()
    assert_converged(report)
    for node_id in THREE:
        assert report.states[node_id].get("k") is None  # deleted everywhere


def test_crdt_counter_converges_to_sum_under_concurrency() -> None:
    def resolvers() -> ResolverRegistry:
        return ResolverRegistry(default=GCounterResolver())

    scenario = Scenario(
        nodes=THREE,
        writes=[
            WriteEvent(0, US, "c", GCounterValue().increment(US, 5)),
            WriteEvent(0, EU, "c", GCounterValue().increment(EU, 3)),
            WriteEvent(0, AP, "c", GCounterValue().increment(AP, 2)),
        ],
        settle_ms=3000,
        fabric=FabricConfig(latency_ms=30),
        resolvers=resolvers,
    )
    report = MultiRegionSimulator(scenario).run()
    assert_converged(report)
    # every node's counter sums all three concurrent increments.
    for node_id in THREE:
        assert report.states[node_id]["c"].value == 10


def test_determinism_same_seed_same_result() -> None:
    def build() -> Scenario:
        return Scenario(
            nodes=THREE,
            writes=[WriteEvent(i * 10, THREE[i % 3], f"k{i}", i) for i in range(20)],
            settle_ms=2000,
            fabric=FabricConfig(latency_ms=30, drop_rate=0.3),
            seed=12345,
        )

    r1 = MultiRegionSimulator(build()).run()
    r2 = MultiRegionSimulator(build()).run()
    assert r1.converged == r2.converged
    assert r1.states == r2.states


def test_lossy_network_still_converges_via_antientropy() -> None:
    """High drop rate: log shipping fails often, anti-entropy must recover it."""
    scenario = Scenario(
        nodes=THREE,
        writes=[WriteEvent(i * 20, THREE[i % 3], f"k{i}", f"v{i}") for i in range(15)],
        settle_ms=6000,
        fabric=FabricConfig(latency_ms=30, drop_rate=0.5),
        seed=7,
    )
    report = MultiRegionSimulator(scenario).run()
    assert_converged(report)


def test_scenario_requires_nodes() -> None:
    with pytest.raises(ValueError):
        MultiRegionSimulator(Scenario(nodes=[]))
