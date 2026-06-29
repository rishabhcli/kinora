"""The gossip round: the periodic push/pull that drives a cluster to convergence.

This is the orchestration glue. Each replica periodically gossips with a peer:
it ships its recent log records (push) and advertises its frontier so the peer
ships back what it is missing (pull). On a healed partition it escalates to a
Merkle repair. The :class:`GossipEngine` wires one :class:`ReplicaNode`, its
:class:`Reconciler`, the :class:`Transport`, and the :class:`PartitionMonitor`
into a single :meth:`tick` the cluster driver / simulator calls each step.

Message kinds carried over the transport (all plain dataclasses):

* :class:`PushMsg` — unsolicited log records (the steady-state stream).
* :class:`PullRequestMsg` — "here is my frontier; send me the rest".
* :class:`PullResponseMsg` — the answering delta.
* :class:`RepairRequestMsg` / :class:`RepairResponseMsg` — Merkle digest +
  the cells in divergent buckets (post-partition catch-up).

The engine is pure given its transport (the in-memory fabric in tests). It does
no real time; the driver advances a clock and calls :meth:`tick(now_ms)`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.distributed.replication.antientropy import (
    MerkleDigest,
    Reconciler,
    SyncRequest,
    SyncResponse,
)
from app.distributed.replication.clock import NodeId
from app.distributed.replication.failure import PartitionEventKind, PartitionMonitor
from app.distributed.replication.log import ReplicationRecord
from app.distributed.replication.node import ReplicaNode
from app.distributed.replication.transport import Message, Transport
from app.distributed.replication.version import VersionVector


@dataclass(frozen=True, slots=True)
class PushMsg:
    records: Sequence[ReplicationRecord]


@dataclass(frozen=True, slots=True)
class PullRequestMsg:
    frontier: VersionVector


@dataclass(frozen=True, slots=True)
class PullResponseMsg:
    records: Sequence[ReplicationRecord]


@dataclass(frozen=True, slots=True)
class RepairRequestMsg:
    digest: MerkleDigest


@dataclass(frozen=True, slots=True)
class RepairResponseMsg:
    response: SyncResponse


@dataclass(frozen=True, slots=True)
class TickReport:
    """What one engine tick did (for observability and the simulator)."""

    node: NodeId
    delivered: int
    applied: int
    pushed: int
    pull_requested: bool
    repairs_requested: int


class GossipEngine:
    """Drives one replica's participation in the gossip protocol."""

    def __init__(
        self,
        node: ReplicaNode,
        transport: Transport,
        peers: Sequence[NodeId],
        monitor: PartitionMonitor | None = None,
    ) -> None:
        self._node = node
        self._reconciler = Reconciler(node)
        self._transport = transport
        self._peers = tuple(p for p in peers if p != node.node)
        self._monitor = monitor
        self._last_pushed = node.frontier()
        self._round = 0

    @property
    def node(self) -> ReplicaNode:
        return self._node

    @property
    def monitor(self) -> PartitionMonitor | None:
        return self._monitor

    # -- inbound ---------------------------------------------------------- #

    def _handle(self, msg: Message, now_ms: int) -> int:
        """Process one inbound message; return records applied."""
        payload = msg.payload
        if isinstance(payload, PushMsg):
            return self._node.ingest_many(payload.records)
        if isinstance(payload, PullRequestMsg):
            response = self._reconciler.answer(SyncRequest(msg.src, payload.frontier))
            self._transport.send_at(
                self._node.node, msg.src, PullResponseMsg(response.records), now_ms
            )
            return 0
        if isinstance(payload, PullResponseMsg):
            return self._node.ingest_many(payload.records)
        if isinstance(payload, RepairRequestMsg):
            repair = self._reconciler.repair(payload.digest)
            self._transport.send_at(
                self._node.node, msg.src, RepairResponseMsg(repair), now_ms
            )
            return 0
        if isinstance(payload, RepairResponseMsg):
            return self._reconciler.apply_response(payload.response)
        return 0

    def deliver(self, messages: Sequence[Message], now_ms: int) -> int:
        """Apply every inbound message addressed to this node."""
        applied = 0
        for msg in messages:
            if msg.dst == self._node.node:
                applied += self._handle(msg, now_ms)
        return applied

    # -- outbound --------------------------------------------------------- #

    def _push_new_records(self, now_ms: int) -> int:
        """Push records appended since the last push to every healthy peer."""
        delta = self._node.delta_since(self._last_pushed)
        if not delta:
            return 0
        targets = self._healthy_peers()
        for peer in targets:
            self._transport.send_at(self._node.node, peer, PushMsg(delta), now_ms)
        self._last_pushed = self._node.frontier()
        return len(delta) * len(targets)

    def _pull_from_peer(self, peer: NodeId, now_ms: int) -> None:
        self._transport.send_at(
            self._node.node, peer, PullRequestMsg(self._node.frontier()), now_ms
        )

    def _request_repairs(self, peers: Sequence[NodeId], now_ms: int) -> int:
        digest = self._reconciler.digest()
        for peer in peers:
            self._transport.send_at(self._node.node, peer, RepairRequestMsg(digest), now_ms)
        return len(peers)

    def _healthy_peers(self) -> list[NodeId]:
        if self._monitor is None:
            return list(self._peers)
        healthy = self._monitor.healthy_peers()
        return [p for p in self._peers if p in healthy]

    def _peer_for_round(self) -> NodeId | None:
        """Round-robin peer selection (deterministic gossip target)."""
        peers = self._healthy_peers()
        if not peers:
            return None
        return peers[self._round % len(peers)]

    # -- the tick --------------------------------------------------------- #

    def tick(self, messages: Sequence[Message], now_ms: int) -> TickReport:
        """One protocol step: deliver inbound, then push + pull + heal-repair.

        Order matters: we apply inbound first so our frontier reflects what we
        just learned, then push our own news and pull from one peer. Healed
        partitions (from the monitor) trigger a Merkle repair request to the
        peers that just came back.
        """
        applied = self.deliver(messages, now_ms)

        # React to liveness transitions: a HEALED peer gets a repair request.
        repairs = 0
        if self._monitor is not None:
            events = self._monitor.observe(now_ms)
            healed = [e.peer for e in events if e.kind is PartitionEventKind.HEALED]
            healed = [p for p in healed if p in self._peers]
            if healed:
                repairs = self._request_repairs(healed, now_ms)

        pushed = self._push_new_records(now_ms)

        pull_target = self._peer_for_round()
        if pull_target is not None:
            self._pull_from_peer(pull_target, now_ms)
        self._round += 1

        return TickReport(
            node=self._node.node,
            delivered=len(messages),
            applied=applied,
            pushed=pushed,
            pull_requested=pull_target is not None,
            repairs_requested=repairs,
        )
