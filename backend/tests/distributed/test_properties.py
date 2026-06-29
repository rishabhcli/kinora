"""Property-based convergence proofs (seeded-random, no external deps).

These tests stress the package's central guarantee — strong eventual
consistency — by generating thousands of randomized operation sequences with a
seeded RNG (deterministic, reproducible, no ``hypothesis`` dependency) and
asserting:

* the CRDT merge laws (commutativity, associativity, idempotence) over random
  value populations;
* state convergence: applying the same record set in many random *legal* orders
  to fresh stores yields byte-identical state;
* end-to-end convergence: the multi-region simulator reaches one identical state
  across replicas for random write/partition timelines.

Each generator iterates many seeds so coverage is broad while every failure is
reproducible from its printed seed.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any, Protocol

from app.distributed.replication.clock import (
    HybridLogicalClock,
    HybridTimestamp,
    ManualClock,
    NodeId,
)
from app.distributed.replication.conflict import (
    GCounterValue,
    LWWResolver,
    MVRegisterValue,
    ORSetValue,
    PNCounterValue,
    ResolverRegistry,
)
from app.distributed.replication.node import ReplicaNode
from app.distributed.replication.simulator import (
    MultiRegionSimulator,
    PartitionEvent,
    Scenario,
    WriteEvent,
    assert_converged,
)
from app.distributed.replication.transport import FabricConfig

NODES = [NodeId("us", "1"), NodeId("eu", "1"), NodeId("ap", "1"), NodeId("sa", "1")]


class Mergeable(Protocol):
    """A state-based CRDT value: it can join with another of its kind."""

    def merge(self, other: Any) -> Any: ...


# --------------------------------------------------------------------------- #
# CRDT law generators
# --------------------------------------------------------------------------- #


def _rand_gcounter(rng: random.Random) -> GCounterValue:
    c = GCounterValue()
    for _ in range(rng.randint(0, 6)):
        c = c.increment(rng.choice(NODES), rng.randint(1, 10))
    return c


def _rand_pncounter(rng: random.Random) -> PNCounterValue:
    c = PNCounterValue()
    for _ in range(rng.randint(0, 6)):
        c = c.add(rng.choice(NODES), rng.randint(-10, 10))
    return c


def _rand_orset(rng: random.Random) -> ORSetValue[str]:
    s: ORSetValue[str] = ORSetValue()
    for i in range(rng.randint(0, 8)):
        element = rng.choice(["a", "b", "c", "d"])
        if rng.random() < 0.6 or not s.contains(element):
            s = s.add(element, f"tag-{i}-{rng.randint(0, 999)}")
        else:
            s = s.remove(element)
    return s


def _rand_mvregister(rng: random.Random) -> MVRegisterValue[str]:
    reg: MVRegisterValue[str] = MVRegisterValue()
    for i in range(rng.randint(0, 5)):
        node = rng.choice(NODES)
        ts = HybridTimestamp(rng.randint(0, 100), rng.randint(0, 5), node)
        reg = reg.write(f"v{i}", ts)
    return reg


_GENERATORS: dict[str, Callable[[random.Random], Mergeable]] = {
    "gcounter": _rand_gcounter,
    "pncounter": _rand_pncounter,
    "orset": _rand_orset,
    "mvregister": _rand_mvregister,
}


def _resolved(label: str, value: Any) -> Any:
    """The observable resolved value of a CRDT (what convergence is judged on)."""
    if label in ("gcounter", "pncounter"):
        return value.value
    if label == "orset":
        return value.elements()
    if label == "mvregister":
        return value.values
    return value


def _equal(label: str, a: Any, b: Any) -> bool:
    """Value-equality that compares CRDTs by their observable resolved value."""
    return bool(_resolved(label, a) == _resolved(label, b))


def test_crdt_merge_commutative_over_random_population() -> None:
    for label, gen in _GENERATORS.items():
        for seed in range(300):
            rng = random.Random(seed)
            a, b = gen(rng), gen(rng)
            assert _equal(label, a.merge(b), b.merge(a)), f"{label} not commutative @ seed {seed}"


def test_crdt_merge_associative_over_random_population() -> None:
    for label, gen in _GENERATORS.items():
        for seed in range(300):
            rng = random.Random(seed + 10_000)
            a, b, c = gen(rng), gen(rng), gen(rng)
            left = a.merge(b).merge(c)
            right = a.merge(b.merge(c))
            assert _equal(label, left, right), f"{label} not associative @ seed {seed}"


def test_crdt_merge_idempotent_over_random_population() -> None:
    for label, gen in _GENERATORS.items():
        for seed in range(300):
            rng = random.Random(seed + 20_000)
            a = gen(rng)
            assert _equal(label, a.merge(a), a), f"{label} not idempotent @ seed {seed}"


def test_random_merge_order_converges() -> None:
    """Folding a random population of CRDT values in any order gives one value."""
    for label, gen in _GENERATORS.items():
        for seed in range(150):
            rng = random.Random(seed + 30_000)
            population = [gen(rng) for _ in range(rng.randint(2, 6))]
            base = population[0]
            for v in population[1:]:
                base = base.merge(v)
            # shuffle and re-fold a few times; result must match.
            for _ in range(4):
                shuffled = list(population)
                rng.shuffle(shuffled)
                acc = shuffled[0]
                for v in shuffled[1:]:
                    acc = acc.merge(v)
                assert _equal(label, acc, base), f"{label} order-dependent @ seed {seed}"


# --------------------------------------------------------------------------- #
# Store-level convergence over random legal delivery orders
# --------------------------------------------------------------------------- #


def _make_node(node: NodeId, clock: ManualClock) -> ReplicaNode:
    return ReplicaNode(
        node, HybridLogicalClock(node, clock), ResolverRegistry(default=LWWResolver())
    )


def test_random_write_logs_converge_across_replicas() -> None:
    """Generate random per-node write logs, ship them in random orders, converge."""
    for seed in range(120):
        rng = random.Random(seed + 40_000)
        nodes = NODES[: rng.randint(2, 4)]
        # each node makes a random sequence of writes to a small keyspace.
        clocks = {n: ManualClock(rng.randint(0, 50)) for n in nodes}
        replicas = {n: _make_node(n, clocks[n]) for n in nodes}
        all_records = []
        for n in nodes:
            for _ in range(rng.randint(1, 6)):
                # advance this node's clock so timestamps spread out.
                clocks[n].advance(rng.randint(1, 20))
                key = rng.choice(["k1", "k2", "k3"])
                if rng.random() < 0.2:
                    all_records.append(replicas[n].delete(key).record)
                else:
                    value = f"{n.region}-{rng.randint(0, 99)}"
                    all_records.append(replicas[n].put(key, value).record)

        # Two fresh replicas ingest all records in two different random orders
        # (respecting per-origin order, which ingest enforces via buffering).
        targets = [_make_node(NodeId("zz", str(i)), ManualClock(0)) for i in range(2)]
        for target in targets:
            shuffled = list(all_records)
            rng.shuffle(shuffled)
            # ingest with retries; buffering handles out-of-order arrival.
            for rec in shuffled:
                target.ingest(rec)
            # drain any still-buffered by re-ingesting (idempotent) until stable.
            for rec in shuffled:
                target.ingest(rec)

        snap0 = {k: (v,) for k, v in targets[0].store.items()}
        snap1 = {k: (v,) for k, v in targets[1].store.items()}
        assert snap0 == snap1, f"replicas diverged @ seed {seed}: {snap0} vs {snap1}"


# --------------------------------------------------------------------------- #
# End-to-end simulator convergence over random timelines
# --------------------------------------------------------------------------- #


def _random_scenario(seed: int) -> Scenario:
    rng = random.Random(seed)
    nodes = NODES[: rng.randint(2, 4)]
    regions = [n.region for n in nodes]
    writes = []
    for i in range(rng.randint(3, 20)):
        node = rng.choice(nodes)
        at = rng.randint(0, 1500)
        key = rng.choice([f"k{j}" for j in range(5)])
        if rng.random() < 0.15:
            writes.append(WriteEvent(at, node, key, None, delete=True))
        else:
            writes.append(WriteEvent(at, node, key, f"{node.region}-{i}"))
    partitions = []
    if len(regions) >= 2 and rng.random() < 0.6:
        a, b = rng.sample(regions, 2)
        down_at = rng.randint(100, 800)
        up_at = down_at + rng.randint(500, 1500)
        partitions = [
            PartitionEvent(down_at, a, b, up=False),
            PartitionEvent(up_at, a, b, up=True),
        ]
    return Scenario(
        nodes=nodes,
        writes=writes,
        partitions=partitions,
        settle_ms=6000,
        fabric=FabricConfig(latency_ms=rng.randint(10, 60), drop_rate=rng.choice([0.0, 0.2, 0.4])),
        heartbeat_timeout_ms=300,
        seed=seed,
    )


def test_random_timelines_always_converge() -> None:
    """Fuzz the simulator: random writes + partitions must always reconverge."""
    for seed in range(60):
        scenario = _random_scenario(seed + 50_000)
        report = MultiRegionSimulator(scenario).run()
        assert_converged(report)


def test_random_timelines_are_deterministic() -> None:
    for seed in range(20):
        scenario_a = _random_scenario(seed + 60_000)
        scenario_b = _random_scenario(seed + 60_000)
        ra = MultiRegionSimulator(scenario_a).run()
        rb = MultiRegionSimulator(scenario_b).run()
        assert ra.states == rb.states, f"non-deterministic @ seed {seed}"
