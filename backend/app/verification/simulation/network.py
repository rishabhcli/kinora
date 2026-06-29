"""A simulated network with injectable latency, reorder, drop, and partition
(kinora.md §6/§12.6 — the api, the broker, the workers, and DashScope talk over a
real network, so a faithful simulator must model one).

The model is a **message bus between named nodes**. A sender hands a message to
:meth:`SimNetwork.send`; the network decides — via :class:`Buggify` against the
run profile — whether to deliver it, when, and in what order relative to other
in-flight messages, then schedules the delivery callback on the
:class:`~app.verification.simulation.core.EventLoop`. Every perturbation is a draw
from the seeded PRNG, so the same seed reproduces the same interleaving.

What it models, and why each matters for Kinora:

* **Latency** (``NET_LATENCY``) — a base RTT plus a randomly-drawn extra delay.
  The scheduler's watermark math assumes work *eventually* lands; latency stresses
  whether the buffer stays healthy when "eventually" is slow.
* **Drop** (``NET_DROP``) — the message never arrives. The sender must time out
  and retry; this is the adversary that proves at-least-once delivery + idempotency
  (§12.1 ``shot_hash``) never double-spends the video budget.
* **Reorder** (``NET_REORDER``) — a later message overtakes an earlier one. This
  is the classic distributed-systems bug source; it proves the control plane does
  not assume FIFO it never guaranteed (e.g. a ``clip_ready`` arriving before its
  ``submitted``).
* **Partition** (``NET_PARTITION``) — a node is cut off bidirectionally for a span
  of virtual time. Messages to/from it during the window are dropped; the design's
  recovery paths (lease reaper, retry) must heal once it lifts.

The network never blocks and never sleeps — a "200ms delay" is `call_after(200, …)`
on the loop. That is what lets a multi-minute reading session with thousands of
messages simulate in a few milliseconds of wall-clock.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.verification.simulation.buggify import Buggify
from app.verification.simulation.core import EventLoop
from app.verification.simulation.faults import FaultKind

#: A delivered-message handler: receives the payload (opaque to the network).
DeliveryHandler = Callable[[object], None]


@dataclass(slots=True)
class _Partition:
    """An active partition window for a node: isolated until ``until_ms``."""

    node: str
    until_ms: int


@dataclass(slots=True)
class NetworkStats:
    """Observable network behaviour for the run report and invariants."""

    sent: int = 0
    delivered: int = 0
    dropped: int = 0
    reordered: int = 0
    partitioned_drops: int = 0
    total_latency_ms: int = 0

    @property
    def delivery_rate(self) -> float:
        """Fraction of sent messages that were delivered."""
        return self.delivered / self.sent if self.sent else 1.0

    @property
    def mean_latency_ms(self) -> float:
        """Mean delivery latency over delivered messages."""
        return self.total_latency_ms / self.delivered if self.delivered else 0.0


class SimNetwork:
    """A deterministic, fault-injecting message bus over the virtual clock.

    Nodes register a delivery handler with :meth:`listen`; senders call
    :meth:`send`. The network consults :class:`Buggify` for each message and
    schedules delivery (or drops it) on the loop. Reordering is modelled by
    occasionally adding a *negative-correlated* extra delay so a later send lands
    before an earlier one — the loop's ``(time, seq)`` ordering then delivers them
    out of program order, exactly as a real reorder would.
    """

    __slots__ = ("_loop", "_buggify", "_handlers", "_partitions", "_base_latency_ms", "stats")

    def __init__(
        self,
        loop: EventLoop,
        buggify: Buggify,
        *,
        base_latency_ms: int = 5,
    ) -> None:
        self._loop = loop
        self._buggify = buggify
        self._handlers: dict[str, DeliveryHandler] = {}
        self._partitions: dict[str, _Partition] = {}
        self._base_latency_ms = base_latency_ms
        self.stats = NetworkStats()

    def listen(self, node: str, handler: DeliveryHandler) -> None:
        """Register ``node``'s delivery handler (last registration wins)."""
        self._handlers[node] = handler

    def _is_partitioned(self, node: str, now_ms: int) -> bool:
        part = self._partitions.get(node)
        if part is None:
            return False
        if now_ms >= part.until_ms:
            # Partition has healed; drop the record so the node is reachable again.
            del self._partitions[node]
            return False
        return True

    def _maybe_open_partition(self, node: str, now_ms: int) -> None:
        """Roll for a fresh partition on ``node`` (idempotent while one is open)."""
        if node in self._partitions:
            return
        span = self._buggify.duration(
            FaultKind.NET_PARTITION, "net.partition", detail=node
        )
        if span > 0:
            self._partitions[node] = _Partition(node=node, until_ms=now_ms + span)

    def send(self, src: str, dst: str, payload: object, *, label: str = "") -> None:
        """Send ``payload`` from ``src`` to ``dst`` over the simulated network.

        Applies (in order): partition check on both endpoints, drop roll, latency
        draw, and reorder draw, then schedules delivery on the loop. A dropped
        message simply never schedules — the sender's own timeout/retry path (if
        any) is what recovers, which is precisely the behaviour under test.
        """
        now = self._loop.clock.now_ms
        self.stats.sent += 1

        # A partition can open at any send; once open it isolates the node both
        # ways until it heals.
        self._maybe_open_partition(dst, now)
        if self._is_partitioned(src, now) or self._is_partitioned(dst, now):
            self.stats.dropped += 1
            self.stats.partitioned_drops += 1
            return

        if self._buggify.should(FaultKind.NET_DROP, "net.send", detail=f"{src}->{dst}"):
            self.stats.dropped += 1
            return

        delay = self._base_latency_ms
        delay += self._buggify.duration(
            FaultKind.NET_LATENCY, "net.latency", detail=f"{src}->{dst}"
        )

        # Reorder: occasionally deliver *sooner* than the base so a later message
        # can overtake an earlier one (the loop orders by absolute time).
        if self._buggify.should(FaultKind.NET_REORDER, "net.reorder", detail=f"{src}->{dst}"):
            # Pull the delivery back toward "now" (but never before it).
            delay = max(0, delay - self._buggify.roll_choice(delay + 1))
            self.stats.reordered += 1

        handler = self._handlers.get(dst)
        if handler is None:
            # No listener (node not up): treat as a drop, the sender must cope.
            self.stats.dropped += 1
            return

        fire_at = now + delay

        def _deliver(t_ms: int, _payload: object = payload, _dst: str = dst) -> None:
            # Re-check partition at *delivery* time: a partition that opened after
            # send still swallows the message (models a link going down mid-flight).
            if self._is_partitioned(_dst, t_ms):
                self.stats.dropped += 1
                self.stats.partitioned_drops += 1
                return
            self.stats.delivered += 1
            self.stats.total_latency_ms += delay
            self._handlers[_dst](_payload)

        self._loop.call_at(fire_at, _deliver, label=label or f"net:{src}->{dst}")

    def heal_all(self) -> None:
        """Lift every active partition immediately (end-of-run convergence)."""
        self._partitions.clear()

    @property
    def open_partitions(self) -> list[str]:
        """Nodes currently isolated by a partition (diagnostics)."""
        now = self._loop.clock.now_ms
        return [n for n, p in self._partitions.items() if now < p.until_ms]


__all__ = [
    "DeliveryHandler",
    "NetworkStats",
    "SimNetwork",
]
