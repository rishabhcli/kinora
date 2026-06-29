"""The inter-region transport seam + a deterministic in-memory fabric.

Replication messages cross regions over a transport. The protocol is written
against the :class:`Transport` interface so production can plug a real RPC/queue
client while the simulator and tests use :class:`InMemoryFabric` — a fully
deterministic message bus with injectable **latency**, **drop**, **reorder**,
and **partition** behaviour. That is how the multi-region simulator proves
convergence under adverse networks without any real I/O.

Messages are opaque payloads addressed ``src -> dst``; the gossip layer defines
their meaning. Delivery is *pull-on-tick*: :meth:`InMemoryFabric.deliver_due`
returns every message whose scheduled arrival time has passed and that is not
blocked by a partition, modelling asynchronous, possibly-reordered delivery.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.distributed.replication.clock import NodeId


@dataclass(frozen=True, slots=True)
class Message:
    """One transport envelope. ``payload`` is opaque to the fabric."""

    src: NodeId
    dst: NodeId
    payload: Any
    #: Monotonic send ordinal, assigned by the fabric (a deterministic tiebreak).
    seq: int = 0


class Transport(ABC):
    """The send/receive seam the gossip layer is written against."""

    @abstractmethod
    def send(self, src: NodeId, dst: NodeId, payload: Any) -> None:
        """Enqueue a message for asynchronous delivery (send time implicit)."""

    def send_at(self, src: NodeId, dst: NodeId, payload: Any, now_ms: int) -> None:
        """Enqueue a message with an explicit send time.

        The gossip layer always uses this so arrival is scheduled relative to a
        deterministic clock. The base implementation ignores ``now_ms`` and
        delegates to :meth:`send`; transports that model latency override it.
        """
        self.send(src, dst, payload)

    @abstractmethod
    def deliver_due(self, now_ms: int) -> list[Message]:
        """Return (and remove) every message deliverable at ``now_ms``."""


@dataclass
class _InFlight:
    message: Message
    arrive_ms: int


class Partition:
    """A directed set of severed region pairs.

    A link ``(a_region, b_region)`` is *down* if either direction is listed.
    Partitions are symmetric by default (sever both directions) but the model
    supports asymmetric (one-way) failures too — common in real outages.
    """

    def __init__(self) -> None:
        self._down: set[tuple[str, str]] = set()

    def sever(self, region_a: str, region_b: str, *, symmetric: bool = True) -> None:
        self._down.add((region_a, region_b))
        if symmetric:
            self._down.add((region_b, region_a))

    def heal(self, region_a: str, region_b: str, *, symmetric: bool = True) -> None:
        self._down.discard((region_a, region_b))
        if symmetric:
            self._down.discard((region_b, region_a))

    def heal_all(self) -> None:
        self._down.clear()

    def is_blocked(self, src: NodeId, dst: NodeId) -> bool:
        return (src.region, dst.region) in self._down

    @property
    def severed_pairs(self) -> frozenset[tuple[str, str]]:
        return frozenset(self._down)


@dataclass
class FabricConfig:
    """Network behaviour knobs for :class:`InMemoryFabric`."""

    #: Fixed one-way latency in ms applied to every message.
    latency_ms: int = 10
    #: Per-(src_region, dst_region) latency overrides (cross-region is slower).
    link_latency_ms: dict[tuple[str, str], int] = field(default_factory=dict)
    #: Fraction [0, 1] of messages silently dropped (sender must retransmit via
    #: anti-entropy). Deterministic via the injected RNG, not wall-clock random.
    drop_rate: float = 0.0


class InMemoryFabric(Transport):
    """A deterministic message bus modelling an async, lossy, partitionable WAN.

    Time is external: the caller advances a clock and calls :meth:`deliver_due`.
    Latency, drops, and partitions are all reproducible given the injected RNG,
    so a simulator run is a pure function of its seed.
    """

    def __init__(
        self,
        config: FabricConfig | None = None,
        partition: Partition | None = None,
        rng: Any | None = None,
    ) -> None:
        self._config = config or FabricConfig()
        self._partition = partition or Partition()
        self._rng = rng
        self._inflight: list[_InFlight] = []
        self._send_seq = 0
        #: Messages dropped by partition while in flight wait here for healing.
        self._held: list[_InFlight] = []

    @property
    def partition(self) -> Partition:
        return self._partition

    @property
    def config(self) -> FabricConfig:
        return self._config

    def _latency(self, src: NodeId, dst: NodeId) -> int:
        return self._config.link_latency_ms.get(
            (src.region, dst.region), self._config.latency_ms
        )

    def _should_drop(self) -> bool:
        """Deterministically decide whether to drop a message (needs an RNG)."""
        if self._config.drop_rate <= 0 or self._rng is None:
            return False
        return bool(self._rng.random() < self._config.drop_rate)

    def send(self, src: NodeId, dst: NodeId, payload: Any) -> None:
        """Send relative to time 0 (latency-only schedule); prefer :meth:`send_at`."""
        self.send_at(src, dst, payload, now_ms=0)

    def send_at(self, src: NodeId, dst: NodeId, payload: Any, now_ms: int) -> None:
        """Send with an explicit send time; arrival = ``now_ms`` + link latency.

        A dropped message is silently discarded — anti-entropy (Merkle
        reconciliation) is what recovers it later, exactly as in a real system.
        """
        if self._should_drop():
            return
        self._send_seq += 1
        msg = Message(src, dst, payload, self._send_seq)
        self._inflight.append(_InFlight(msg, now_ms + self._latency(src, dst)))

    def deliver_due(self, now_ms: int) -> list[Message]:
        """Deliver every message arrived by ``now_ms`` and not blocked by a partition.

        Partition-blocked messages are *held* (not dropped) and retried once the
        link heals — modelling a queue that buffers across a transient outage.
        Delivered messages are returned sorted by (arrive_ms, send seq) for a
        deterministic, possibly-reordered-vs-send order.
        """
        due: list[_InFlight] = []
        still: list[_InFlight] = []
        for f in self._inflight:
            if f.arrive_ms <= now_ms:
                if self._partition.is_blocked(f.message.src, f.message.dst):
                    self._held.append(f)
                else:
                    due.append(f)
            else:
                still.append(f)
        # Re-check held messages: a healed link lets them through now.
        retry_still: list[_InFlight] = []
        for f in self._held:
            if not self._partition.is_blocked(f.message.src, f.message.dst):
                due.append(f)
            else:
                retry_still.append(f)
        self._held = retry_still
        self._inflight = still
        due.sort(key=lambda f: (f.arrive_ms, f.message.seq))
        return [f.message for f in due]

    def pending(self) -> int:
        """Count of messages still in flight or held by a partition."""
        return len(self._inflight) + len(self._held)


class DirectTransport(Transport):
    """A zero-latency, lossless transport with explicit per-node mailboxes.

    Useful for the simplest convergence tests where the network model is not
    under test — messages are delivered in send order on the next drain.
    """

    def __init__(self) -> None:
        self._queue: list[Message] = []
        self._seq = 0

    def send(self, src: NodeId, dst: NodeId, payload: Any) -> None:
        self._seq += 1
        self._queue.append(Message(src, dst, payload, self._seq))

    def deliver_due(self, now_ms: int) -> list[Message]:
        drained = list(self._queue)
        self._queue.clear()
        return drained

    def drain(self) -> list[Message]:
        return self.deliver_due(0)


def messages_for(messages: Iterable[Message], dst: NodeId) -> list[Message]:
    """Filter ``messages`` to those addressed to ``dst`` (a routing helper)."""
    return [m for m in messages if m.dst == dst]
