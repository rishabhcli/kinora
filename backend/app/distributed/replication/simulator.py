"""A deterministic multi-region simulator that *proves* convergence.

Everything in this package is pure; this module composes it into a runnable,
fully-deterministic model of a geo-distributed cluster and the adverse network
it lives on, so a test can assert the central property: **after activity stops
and the network heals, every replica holds byte-identical state** (strong
eventual consistency), and along the way bounded staleness / quorum semantics
hold.

The simulator is a discrete-event loop driven by a single :class:`ManualClock`
shared notion of time (per-node clock *skew* is modelled by giving each node its
own offset clock). Each step it: advances time, lets the fabric deliver due
messages, runs every node's :class:`GossipEngine.tick`, and applies any injected
events (client writes, partitions, heals). Because the fabric, the RNG, and the
clocks are all explicit, a run is a pure function of its :class:`Scenario`.

* :class:`Scenario` — the declarative input: regions, the event timeline
  (writes / partition / heal), tick cadence, network config, RNG seed.
* :class:`MultiRegionSimulator` — runs a scenario to quiescence and exposes the
  per-node final state plus a convergence verdict.
* :func:`assert_converged` — the reusable oracle the property tests call.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.distributed.replication.clock import (
    HybridLogicalClock,
    ManualClock,
    NodeId,
)
from app.distributed.replication.conflict import ResolverRegistry
from app.distributed.replication.failure import FailureDetector, PartitionMonitor
from app.distributed.replication.gossip import GossipEngine
from app.distributed.replication.node import ReplicaNode
from app.distributed.replication.store import Cell
from app.distributed.replication.transport import (
    FabricConfig,
    InMemoryFabric,
    Partition,
)

# --------------------------------------------------------------------------- #
# Scenario description
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class WriteEvent:
    """A client write applied to a specific node at a specific time."""

    at_ms: int
    node: NodeId
    key: str
    value: Any
    delete: bool = False


@dataclass(frozen=True, slots=True)
class PartitionEvent:
    """Sever (``up=False``) or heal (``up=True``) a region pair at a time."""

    at_ms: int
    region_a: str
    region_b: str
    up: bool


@dataclass(frozen=True, slots=True)
class Scenario:
    """A fully-declarative simulation input (deterministic given its fields)."""

    nodes: Sequence[NodeId]
    writes: Sequence[WriteEvent] = ()
    partitions: Sequence[PartitionEvent] = ()
    #: How long to keep ticking after the last event, to reach quiescence.
    settle_ms: int = 2_000
    #: Simulation step in ms.
    tick_ms: int = 50
    #: Network behaviour.
    fabric: FabricConfig = field(default_factory=FabricConfig)
    #: Per-node clock offset (skew) in ms, keyed by node.
    clock_skew_ms: Mapping[NodeId, int] = field(default_factory=dict)
    #: Heartbeat timeout for the failure detector.
    heartbeat_timeout_ms: int = 500
    seed: int = 0
    #: Builds the resolver registry each node uses (must be identical per node).
    resolvers: Callable[[], ResolverRegistry] | None = None


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ConvergenceReport:
    """The outcome of a run, including the convergence verdict and divergences."""

    converged: bool
    ticks: int
    final_time_ms: int
    #: For each key, the set of distinct (value, deleted) cells across nodes.
    divergent_keys: Mapping[str, frozenset[tuple[Any, bool]]]
    #: Per-node visible keyspace ({key: value}) at the end.
    states: Mapping[NodeId, Mapping[str, Any]]

    @property
    def is_converged(self) -> bool:
        return self.converged


# --------------------------------------------------------------------------- #
# Simulator
# --------------------------------------------------------------------------- #


class MultiRegionSimulator:
    """Runs a :class:`Scenario` to quiescence over a deterministic fabric."""

    def __init__(self, scenario: Scenario) -> None:
        if not scenario.nodes:
            raise ValueError("a scenario needs at least one node")
        self._scenario = scenario
        self._rng = random.Random(scenario.seed)
        self._partition = Partition()
        self._fabric = InMemoryFabric(scenario.fabric, self._partition, rng=self._rng)
        self._clocks: dict[NodeId, ManualClock] = {}
        self._nodes: dict[NodeId, ReplicaNode] = {}
        self._engines: dict[NodeId, GossipEngine] = {}
        self._build_cluster()

    def _build_cluster(self) -> None:
        make_resolvers = self._scenario.resolvers or _default_resolvers
        peers = list(self._scenario.nodes)
        for node_id in self._scenario.nodes:
            skew = self._scenario.clock_skew_ms.get(node_id, 0)
            clock = ManualClock(skew)
            self._clocks[node_id] = clock
            hlc = HybridLogicalClock(node_id, clock)
            node = ReplicaNode(node_id, hlc, make_resolvers())
            self._nodes[node_id] = node
            monitor = PartitionMonitor(
                [p for p in peers if p != node_id],
                FailureDetector(self._scenario.heartbeat_timeout_ms),
            )
            self._engines[node_id] = GossipEngine(node, self._fabric, peers, monitor)

    @property
    def nodes(self) -> Mapping[NodeId, ReplicaNode]:
        return self._nodes

    def _advance_clocks(self, sim_time_ms: int) -> None:
        for node_id, clock in self._clocks.items():
            skew = self._scenario.clock_skew_ms.get(node_id, 0)
            clock.set(sim_time_ms + skew)

    def _apply_writes_at(self, sim_time_ms: int, tick_ms: int) -> None:
        for ev in self._scenario.writes:
            if sim_time_ms <= ev.at_ms < sim_time_ms + tick_ms:
                node = self._nodes[ev.node]
                if ev.delete:
                    node.delete(ev.key)
                else:
                    node.put(ev.key, ev.value)

    def _apply_partitions_at(self, sim_time_ms: int, tick_ms: int) -> None:
        for ev in self._scenario.partitions:
            if sim_time_ms <= ev.at_ms < sim_time_ms + tick_ms:
                if ev.up:
                    self._partition.heal(ev.region_a, ev.region_b)
                else:
                    self._partition.sever(ev.region_a, ev.region_b)

    def _feed_heartbeats(self, sim_time_ms: int) -> None:
        """Each non-partitioned peer pair exchanges a heartbeat this tick."""
        for node_id, engine in self._engines.items():
            monitor = engine.monitor
            if monitor is None:
                continue
            for peer in self._scenario.nodes:
                if peer == node_id:
                    continue
                if not self._partition.is_blocked(peer, node_id):
                    monitor.heartbeat(peer, sim_time_ms)

    def run(self) -> ConvergenceReport:
        """Run to quiescence and return the convergence verdict."""
        last_event = 0
        for write in self._scenario.writes:
            last_event = max(last_event, write.at_ms)
        for part in self._scenario.partitions:
            last_event = max(last_event, part.at_ms)
        end_ms = last_event + self._scenario.settle_ms
        tick_ms = self._scenario.tick_ms

        ticks = 0
        sim_time = 0
        while sim_time <= end_ms:
            self._advance_clocks(sim_time)
            self._apply_partitions_at(sim_time, tick_ms)
            self._apply_writes_at(sim_time, tick_ms)
            self._feed_heartbeats(sim_time)
            due = self._fabric.deliver_due(sim_time)
            for engine in self._engines.values():
                engine.tick(due, sim_time)
            ticks += 1
            sim_time += tick_ms

        return self._report(ticks, sim_time - tick_ms)

    def _report(self, ticks: int, final_time_ms: int) -> ConvergenceReport:
        # Gather every cell across nodes, keyed by key.
        per_key: dict[str, set[tuple[Any, bool]]] = {}
        states: dict[NodeId, dict[str, Any]] = {}
        all_keys: set[str] = set()
        snapshots: dict[NodeId, Mapping[str, Cell]] = {}
        for node_id, node in self._nodes.items():
            snap = node.store.snapshot()
            snapshots[node_id] = snap
            all_keys |= set(snap)
            states[node_id] = dict(node.store.items())
        for key in all_keys:
            distinct: set[tuple[Any, bool]] = set()
            for snap in snapshots.values():
                cell = snap.get(key)
                if cell is None:
                    distinct.add((None, True))  # absent == tombstone-equivalent
                else:
                    distinct.add((_freeze(cell.value) if not cell.deleted else None, cell.deleted))
            per_key[key] = distinct
        divergent = {k: frozenset(v) for k, v in per_key.items() if len(v) > 1}
        return ConvergenceReport(
            converged=len(divergent) == 0,
            ticks=ticks,
            final_time_ms=final_time_ms,
            divergent_keys=divergent,
            states=states,
        )


def _freeze(value: Any) -> Any:
    """Best-effort hashable view of a value for divergence comparison."""
    try:
        hash(value)
        return value
    except TypeError:
        return repr(value)


def _default_resolvers() -> ResolverRegistry:
    from app.distributed.replication.conflict import LWWResolver

    return ResolverRegistry(default=LWWResolver())


def assert_converged(report: ConvergenceReport) -> None:
    """Raise an informative ``AssertionError`` if the run did not converge."""
    if not report.converged:
        raise AssertionError(
            f"replicas diverged after {report.ticks} ticks: {dict(report.divergent_keys)}"
        )
