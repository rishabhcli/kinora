"""Tests for the per-replica store and apply path (store.py)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from app.distributed.replication.clock import HybridTimestamp, NodeId
from app.distributed.replication.conflict import (
    GCounterResolver,
    GCounterValue,
    LWWResolver,
    ResolverRegistry,
)
from app.distributed.replication.log import ReplicationRecord, WriteOp
from app.distributed.replication.store import KeyAffinity, ReplicaStore
from app.distributed.replication.version import VersionVector

A = NodeId("us", "a")
B = NodeId("eu", "b")


def lww_registry() -> ResolverRegistry:
    return ResolverRegistry(default=LWWResolver())


def set_rec(origin: NodeId, seq: int, key: str, value: Any, wall: int) -> ReplicationRecord:
    return ReplicationRecord(
        origin=origin,
        seq=seq,
        timestamp=HybridTimestamp(wall, 0, origin),
        op=WriteOp.set(key, value),
    )


def del_rec(origin: NodeId, seq: int, key: str, wall: int) -> ReplicationRecord:
    return ReplicationRecord(
        origin=origin,
        seq=seq,
        timestamp=HybridTimestamp(wall, 0, origin),
        op=WriteOp.delete(key),
    )


def test_apply_sets_value_and_advances_frontier() -> None:
    store = ReplicaStore(A, lww_registry())
    changed = store.apply(set_rec(A, 1, "k", 42, 10))
    assert changed
    assert store.get("k") == 42
    assert store.frontier() == VersionVector.of({A: 1})


def test_apply_is_idempotent() -> None:
    store = ReplicaStore(A, lww_registry())
    record = set_rec(A, 1, "k", 42, 10)
    store.apply(record)
    assert not store.apply(record)  # already covered
    assert store.get("k") == 42


def test_lww_higher_timestamp_wins_regardless_of_order() -> None:
    early = set_rec(A, 1, "k", "early", 10)
    late = set_rec(B, 1, "k", "late", 20)
    store1 = ReplicaStore(A, lww_registry())
    store1.apply(early)
    store1.apply(late)
    store2 = ReplicaStore(A, lww_registry())
    store2.apply(late)
    store2.apply(early)
    assert store1.get("k") == store2.get("k") == "late"


def test_delete_then_concurrent_write_resolves_by_timestamp() -> None:
    store = ReplicaStore(A, lww_registry())
    store.apply(set_rec(A, 1, "k", "v", 10))
    store.apply(del_rec(B, 1, "k", 20))  # later delete wins
    assert store.get("k") is None
    # a still-later write resurrects.
    store.apply(set_rec(A, 2, "k", "alive", 30))
    assert store.get("k") == "alive"


def test_crdt_counter_merges_concurrent_increments() -> None:
    reg = ResolverRegistry(default=GCounterResolver())
    store = ReplicaStore(A, reg)
    store.apply(set_rec(A, 1, "c", GCounterValue().increment(A, 3), 10))
    store.apply(set_rec(B, 1, "c", GCounterValue().increment(B, 5), 5))
    assert store.get("c").value == 8  # both survive, timestamp order irrelevant


def test_keys_excludes_tombstones() -> None:
    store = ReplicaStore(A, lww_registry())
    store.apply(set_rec(A, 1, "a", 1, 10))
    store.apply(set_rec(A, 2, "b", 2, 20))
    store.apply(del_rec(A, 3, "a", 30))
    assert store.keys() == {"b"}
    assert dict(store.items()) == {"b": 2}


def test_affinity_metadata_roundtrips() -> None:
    store = ReplicaStore(A, lww_registry())
    aff = KeyAffinity(home_region="us", replicas=frozenset({"us", "eu"}))
    store.set_affinity("book:1", aff)
    assert store.affinity("book:1") == aff
    assert aff.is_replicated_in("eu")
    assert not aff.is_replicated_in("ap")


def _valid_interleavings(
    segments: list[list[ReplicationRecord]],
) -> Iterator[list[ReplicationRecord]]:
    """Yield every interleaving of per-origin segments that preserves each segment's order.

    These are exactly the delivery orders an active-active node can legally see
    (per-origin FIFO, cross-origin arbitrary). Convergence must hold over all.
    """
    cursors = tuple(0 for _ in segments)
    stack: list[tuple[tuple[int, ...], list[ReplicationRecord]]] = [(cursors, [])]
    while stack:
        cur, acc = stack.pop()
        if all(cur[i] == len(segments[i]) for i in range(len(segments))):
            yield acc
            continue
        for i, seg in enumerate(segments):
            if cur[i] < len(seg):
                nxt = list(cur)
                nxt[i] += 1
                stack.append((tuple(nxt), [*acc, seg[cur[i]]]))


def test_apply_all_orderings_converge_to_identical_state() -> None:
    """Convergence: every legal delivery interleaving yields identical cells."""
    seg_a = [set_rec(A, 1, "x", "a1", 10), set_rec(A, 2, "x", "a2", 30)]
    seg_b = [set_rec(B, 1, "x", "b1", 20), del_rec(B, 2, "y", 25)]
    snapshots = set()
    count = 0
    for order in _valid_interleavings([seg_a, seg_b]):
        store = ReplicaStore(A, lww_registry())
        for r in order:
            store.apply(r)
        snapshots.add(
            tuple(sorted((k, c.value, c.deleted) for k, c in store.snapshot().items()))
        )
        count += 1
    assert count == 6  # C(4,2) interleavings of two length-2 segments
    assert len(snapshots) == 1
