"""Anti-entropy: gossip digests + Merkle reconciliation to drive convergence.

Async log shipping (push) handles the steady state; anti-entropy (pull) closes
every gap it leaves — dropped messages, post-partition divergence, a freshly
joined replica. This module is the pure protocol logic that decides *what* a
pair of replicas exchange; the transport carries it and the
:mod:`app.distributed.replication.simulator` schedules it.

Two complementary mechanisms:

* **Version-vector delta exchange** (:func:`plan_delta_sync`) — cheap and
  exact: a replica advertises its applied :class:`VersionVector`; the peer ships
  every log record beyond it. This is the common path and is enough on its own
  whenever both peers still hold the relevant log tail.
* **Merkle reconciliation** (:func:`plan_merkle_repair`) — the safety net when
  logs have been compacted or the divergence is large: the two replicas compare
  fixed-shape Merkle trees over their *materialized* keyspace and exchange only
  the keys in mismatching buckets, re-deriving records to ship from the store.

:class:`Reconciler` wraps a :class:`ReplicaNode` and exposes the messages it
would send and how it applies an inbound sync — the unit the gossip round and
the simulator drive.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.distributed.replication.clock import NodeId
from app.distributed.replication.log import OpKind, ReplicationRecord, WriteOp
from app.distributed.replication.merkle import (
    MerkleTree,
    bucket_of,
    build_merkle,
    diff_buckets,
)
from app.distributed.replication.node import ReplicaNode
from app.distributed.replication.store import TOMBSTONE, Cell
from app.distributed.replication.version import VersionVector


@dataclass(frozen=True, slots=True)
class SyncRequest:
    """One replica's advertisement of what it has, so a peer can compute a delta."""

    requester: NodeId
    frontier: VersionVector


@dataclass(frozen=True, slots=True)
class SyncResponse:
    """The records a peer ships in answer to a :class:`SyncRequest`.

    ``is_repair`` distinguishes the two paths: a delta response carries real,
    strictly-ordered log records (applied via the node's causal ingest); a
    repair response carries cells re-derived from the materialized store (merged
    by value/timestamp, no strict log order).
    """

    responder: NodeId
    records: Sequence[ReplicationRecord]
    is_repair: bool = False

    @property
    def is_empty(self) -> bool:
        return len(self.records) == 0


@dataclass(frozen=True, slots=True)
class MerkleDigest:
    """A replica's keyspace fingerprint tree, advertised for repair."""

    owner: NodeId
    tree: MerkleTree


def plan_delta_sync(local: ReplicaNode, request: SyncRequest) -> SyncResponse:
    """Compute the records ``local`` should ship to satisfy ``request``.

    Exactly the version-vector delta: every record beyond the requester's
    frontier, in causal-safe (timestamp) order. Idempotent and safe to repeat.
    """
    records = local.delta_since(request.frontier)
    return SyncResponse(local.node, records)


def fingerprints_of(node: ReplicaNode) -> dict[str, str]:
    """A ``{key: fingerprint}`` map of ``node``'s store for Merkle comparison.

    The fingerprint encodes the cell's converged write timestamp *and* its
    deleted flag, so two replicas agree on a bucket iff they hold byte-identical
    cells (including tombstones — a present key and a tombstoned key differ).
    """
    out: dict[str, str] = {}
    for key, cell in node.store.snapshot().items():
        ts = cell.timestamp
        flag = "D" if cell.deleted else "P"
        out[key] = f"{flag}:{ts.wall_ms}.{ts.logical}.{ts.node}"
    return out


def merkle_digest(node: ReplicaNode, *, arity: int = 16, depth: int = 4) -> MerkleDigest:
    return MerkleDigest(node.node, build_merkle(fingerprints_of(node), arity=arity, depth=depth))


def plan_merkle_repair(local: ReplicaNode, remote_digest: MerkleDigest) -> SyncResponse:
    """Ship the cells in buckets where ``local`` and the remote digest disagree.

    Re-derives a :class:`ReplicationRecord` per divergent key from the local
    store so the peer can merge it through its resolver. These repair records
    carry the *origin and applied sequence* of the local cell's last writer when
    known; when the origin's log tail is gone we synthesize an origin-tagged
    record from the materialized cell, which still merges correctly because
    resolution is by value/timestamp, not by log position.
    """
    local_tree = build_merkle(
        fingerprints_of(local),
        arity=remote_digest.tree.arity,
        depth=remote_digest.tree.depth,
    )
    divergent = diff_buckets(local_tree, remote_digest.tree)
    if not divergent:
        return SyncResponse(local.node, (), is_repair=True)
    records: list[ReplicationRecord] = []
    for key, cell in local.store.snapshot().items():
        bucket = bucket_of(key, remote_digest.tree.arity, remote_digest.tree.depth)
        if bucket not in divergent:
            continue
        op = WriteOp.delete(key) if cell.deleted else WriteOp.set(key, cell.value)
        # The record is tagged with the cell's winning timestamp's node as
        # origin; the receiver merges it by value/timestamp (repair path), so
        # the seq is informational only and never claims a strict log slot.
        records.append(
            ReplicationRecord(
                origin=cell.timestamp.node,
                seq=0,
                timestamp=cell.timestamp,
                op=op,
                deps=VersionVector.empty(),
            )
        )
    return SyncResponse(local.node, records, is_repair=True)


class Reconciler:
    """Anti-entropy driver bound to one :class:`ReplicaNode`.

    Wraps the planning functions and applies inbound syncs. The
    :mod:`~app.distributed.replication.gossip` round and the simulator call
    :meth:`make_request`, :meth:`answer`, and :meth:`apply_response`.
    """

    def __init__(self, node: ReplicaNode) -> None:
        self._node = node

    @property
    def node(self) -> ReplicaNode:
        return self._node

    def make_request(self) -> SyncRequest:
        return SyncRequest(self._node.node, self._node.frontier())

    def answer(self, request: SyncRequest) -> SyncResponse:
        return plan_delta_sync(self._node, request)

    def digest(self, *, arity: int = 16, depth: int = 4) -> MerkleDigest:
        return merkle_digest(self._node, arity=arity, depth=depth)

    def repair(self, remote_digest: MerkleDigest) -> SyncResponse:
        return plan_merkle_repair(self._node, remote_digest)

    def apply_response(self, response: SyncResponse) -> int:
        """Ingest the shipped records; return count applied.

        Delta responses use the node's strict causal ingest (real log records).
        Repair responses merge each cell into the store by value/timestamp,
        bypassing the strict log — idempotent and convergent.
        """
        if response.is_repair:
            return self._apply_repair(response)
        return self._node.ingest_many(response.records)

    def _apply_repair(self, response: SyncResponse) -> int:
        applied = 0
        store = self._node.store
        for record in response.records:
            cell = (
                Cell(TOMBSTONE, record.timestamp, deleted=True)
                if record.op.kind is OpKind.DELETE
                else Cell(record.op.value, record.timestamp)
            )
            if store.merge_cell(record.key, cell):
                applied += 1
        return applied
