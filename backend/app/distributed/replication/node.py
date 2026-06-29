"""The replica node: the unit of active-active replication.

A :class:`ReplicaNode` ties together one region's local clock, replication log,
materialized store, and a causal-delivery buffer. It is the object the protocol,
anti-entropy, and the simulator drive. Responsibilities:

* **Local writes** (:meth:`put` / :meth:`delete`) — stamp a write with a fresh
  :class:`HybridTimestamp`, append it to this node's log segment with the
  current applied frontier as its causal dependency, and apply it locally. The
  returned :class:`WriteReceipt` is what consistency-level acknowledgement
  counting (in :mod:`app.distributed.replication.consistency`) consumes.
* **Remote ingestion** (:meth:`ingest`) — accept a foreign
  :class:`ReplicationRecord`, fold its stamp into the local clock, and either
  apply it (if causally ready) or park it in the delivery buffer until its
  dependencies arrive. Parked records are re-checked after every ingest, so a
  causal stream that arrives out of order still delivers in causal order.
* **Digest / delta** — expose :meth:`frontier` and :meth:`delta_since` so the
  gossip and anti-entropy layers can compute "what does my peer miss?".

Pure given its injected clock; no network, no storage. The transport and the
gossip loop live in sibling modules and call this surface.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.distributed.replication.clock import HybridLogicalClock, HybridTimestamp, NodeId
from app.distributed.replication.conflict import ResolverRegistry
from app.distributed.replication.log import (
    OpKind,
    ReplicationLog,
    ReplicationRecord,
    WriteOp,
)
from app.distributed.replication.store import KeyAffinity, ReplicaStore
from app.distributed.replication.version import VersionVector


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    """Proof a local write was durably accepted at its origin node."""

    record: ReplicationRecord

    @property
    def node(self) -> NodeId:
        return self.record.origin

    @property
    def timestamp(self) -> HybridTimestamp:
        return self.record.timestamp


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of ingesting one remote record."""

    accepted: bool
    applied: int  # how many records (this one + unblocked) became durable
    buffered: bool  # parked pending dependencies


class ReplicaNode:
    """One region's replica: clock + log + store + causal delivery buffer."""

    def __init__(
        self,
        node: NodeId,
        clock: HybridLogicalClock,
        resolvers: ResolverRegistry,
    ) -> None:
        if clock.node != node:
            raise ValueError("clock node identity must match the replica node")
        self._node = node
        self._clock = clock
        self._log = ReplicationLog()
        self._store = ReplicaStore(node, resolvers)
        # Records received but not yet causally ready, keyed by (origin, seq).
        self._buffer: dict[tuple[NodeId, int], ReplicationRecord] = {}

    # -- identity / inspection -------------------------------------------- #

    @property
    def node(self) -> NodeId:
        return self._node

    @property
    def region(self) -> str:
        return self._node.region

    @property
    def store(self) -> ReplicaStore:
        return self._store

    @property
    def log(self) -> ReplicationLog:
        return self._log

    def frontier(self) -> VersionVector:
        """The version vector of records this node has *applied*."""
        return self._store.frontier()

    def buffered_count(self) -> int:
        return len(self._buffer)

    def get(self, key: str) -> Any:
        return self._store.get(key)

    def set_affinity(self, key: str, affinity: KeyAffinity) -> None:
        self._store.set_affinity(key, affinity)

    # -- local writes ----------------------------------------------------- #

    def put(self, key: str, value: Any) -> WriteReceipt:
        return self._local_write(WriteOp.set(key, value))

    def delete(self, key: str) -> WriteReceipt:
        return self._local_write(WriteOp.delete(key))

    def _local_write(self, op: WriteOp) -> WriteReceipt:
        timestamp = self._clock.now()
        seq = self._log.next_seq(self._node)
        # Dependencies are everything applied *before* this write (excluding it).
        deps = self._store.frontier()
        record = ReplicationRecord(
            origin=self._node,
            seq=seq,
            timestamp=timestamp,
            op=op,
            deps=deps,
        )
        self._log.append(record)
        self._store.apply(record)
        return WriteReceipt(record)

    # -- remote ingestion ------------------------------------------------- #

    def ingest(self, record: ReplicationRecord) -> IngestResult:
        """Accept a remote record; apply now or buffer until causally ready.

        Folds the record's stamp into the local clock (keeping causality across
        nodes), then attempts delivery. Buffered records are drained whenever a
        new ingest unblocks them.
        """
        # Already known? (idempotent — covered by frontier or already buffered.)
        if self._store.frontier().includes(record.origin, record.seq):
            return IngestResult(accepted=False, applied=0, buffered=False)
        if (record.origin, record.seq) in self._buffer:
            return IngestResult(accepted=False, applied=0, buffered=True)

        self._clock.recv(record.timestamp)
        self._buffer[(record.origin, record.seq)] = record
        applied = self._drain()
        buffered = (record.origin, record.seq) in self._buffer
        return IngestResult(accepted=True, applied=applied, buffered=buffered)

    def ingest_many(self, records: Iterable[ReplicationRecord]) -> int:
        """Ingest a batch (e.g. an anti-entropy delta); return total applied."""
        total = 0
        for record in records:
            total += self.ingest(record).applied
        return total

    def _drain(self) -> int:
        """Apply every buffered record that is now causally ready; repeat to fixpoint."""
        applied = 0
        progress = True
        while progress:
            progress = False
            for key, record in list(self._buffer.items()):
                if record.causally_ready(self._store.frontier()):
                    self._ensure_logged(record)
                    self._store.apply(record)
                    del self._buffer[key]
                    applied += 1
                    progress = True
        return applied

    def _ensure_logged(self, record: ReplicationRecord) -> None:
        """Append a foreign record to its origin segment if not already present.

        Because we only apply causally-ready records (no per-origin gap), the
        append is always the next sequence for that origin — the log invariant
        holds for replicated records exactly as for local ones.
        """
        if self._log.next_seq(record.origin) == record.seq:
            self._log.append(record)

    # -- digests for gossip / anti-entropy -------------------------------- #

    def delta_since(self, frontier: VersionVector) -> list[ReplicationRecord]:
        """Records this node has that the holder of ``frontier`` has not seen."""
        return self._log.delta_since(frontier)

    def has_op(self, key: str) -> bool:
        return self._store.cell(key) is not None and not self._store.cell(key).deleted  # type: ignore[union-attr]

    def op_kind_of_last(self, key: str) -> OpKind | None:
        cell = self._store.cell(key)
        if cell is None:
            return None
        return OpKind.DELETE if cell.deleted else OpKind.SET
