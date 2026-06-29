"""Hybrid-logical-clock timestamping for the geo-distributed data layer.

This is the *replication* HLC, distinct from the canon-versioning clock in
:mod:`app.memory.crdt`. It is region-aware (the node identity is a
``(region, node)`` pair, not a bare actor string), carries a configurable
**bounded clock skew** guard so a wildly-wrong wall clock cannot poison the
logical order, and exposes the richer surface the replication protocol,
anti-entropy, and the multi-region simulator need:

* :class:`NodeId` — a stable ``region/node`` identity used everywhere as the
  total-order tiebreak and the unit of region affinity / routing.
* :class:`HybridTimestamp` — ``(wall_ms, logical, node)``; a monotone, causally
  consistent, *globally totally ordered* timestamp. Two timestamps minted on
  different nodes never compare equal, so every conflict resolves the same way
  on every replica.
* :class:`HybridLogicalClock` — a stateful per-node generator implementing the
  Kulkarni et al. HLC update rules (`now`, `send`, `recv`) with a skew clamp.
* :class:`ManualClock` — a deterministic, injectable wall-clock source for the
  simulator and property tests (no real time anywhere in this package).

Everything is pure given its injected ``PhysicalClock`` (a ``Callable[[], int]``
returning epoch milliseconds), so the distributed-time semantics are provable by
unit tests: monotonicity, causality (`send`/`recv` strictly advances), and a
total order that is stable across nodes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

#: A source of physical wall-clock time, in epoch milliseconds. Injected so the
#: whole package is deterministic under test and in the simulator.
PhysicalClock = Callable[[], int]

#: Default bound (ms) on how far ahead of local physical time a *received*
#: timestamp may pull the logical clock before we refuse to track it. A remote
#: node whose clock is more than this far ahead is treated as faulty; we still
#: stay monotone locally but clamp the borrowed wall component.
DEFAULT_MAX_SKEW_MS: Final[int] = 5 * 60 * 1000  # five minutes


@dataclass(frozen=True, slots=True, order=True)
class NodeId:
    """A stable replica identity: ``region`` then ``node``.

    Ordered ``(region, node)`` so it is a deterministic, total tiebreak for two
    timestamps that share a wall and logical component. ``region`` is the unit
    of geo-affinity and routing; ``node`` distinguishes replicas within a region.
    """

    region: str
    node: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.region}/{self.node}"

    @classmethod
    def parse(cls, text: str) -> NodeId:
        """Parse a ``region/node`` string (inverse of :meth:`__str__`)."""
        region, _, node = text.partition("/")
        if not node:
            raise ValueError(f"not a region/node identity: {text!r}")
        return cls(region, node)


@dataclass(frozen=True, slots=True, order=True)
class HybridTimestamp:
    """A globally totally-ordered hybrid-logical timestamp.

    Field order ``(wall_ms, logical, node)`` *is* the comparison order, via
    ``order=True``. The ``node`` tiebreak guarantees the order is **total**:
    two timestamps are equal iff every component matches, which for distinct
    nodes is impossible — so LWW and anti-entropy resolve identically on every
    replica regardless of delivery order.

    ``wall_ms`` keeps timestamps human-meaningful (it tracks physical time);
    ``logical`` disambiguates events that share a wall tick and preserves
    causality when the physical clock stalls or two nodes share a millisecond.
    """

    wall_ms: int
    logical: int
    node: NodeId

    def happens_before(self, other: HybridTimestamp) -> bool:
        """True iff this timestamp strictly precedes ``other`` in the total order."""
        return self < other

    def with_node(self, node: NodeId) -> HybridTimestamp:
        """Return a copy stamped for ``node`` (used when a node re-mints on recv)."""
        return HybridTimestamp(self.wall_ms, self.logical, node)

    @property
    def encoded(self) -> tuple[int, int, str]:
        """A JSON-friendly, order-preserving encoding for the log / wire format."""
        return (self.wall_ms, self.logical, str(self.node))

    @classmethod
    def decode(cls, encoded: tuple[int, int, str]) -> HybridTimestamp:
        wall_ms, logical, node = encoded
        return cls(wall_ms, logical, NodeId.parse(node))


class HybridLogicalClock:
    """A stateful HLC generator owned by exactly one node.

    Implements the standard HLC rules. ``now`` is for a local event; ``send`` is
    ``now`` plus the stamp to put on the wire; ``recv`` folds a received remote
    stamp into local time. The internal ``(wall, logical)`` pair is monotone
    non-decreasing across all three operations, so every stamp this clock ever
    issues strictly dominates the previous one in the total order.

    The skew clamp protects causality from a misbehaving peer: a remote wall
    more than ``max_skew_ms`` ahead of local physical time is not adopted as the
    new wall (we keep local time and bump the logical counter instead), so one
    bad clock cannot drag the whole region's timeline into the future.
    """

    __slots__ = ("_node", "_physical", "_wall", "_logical", "_max_skew_ms")

    def __init__(
        self,
        node: NodeId,
        physical: PhysicalClock,
        *,
        max_skew_ms: int = DEFAULT_MAX_SKEW_MS,
    ) -> None:
        self._node = node
        self._physical = physical
        self._wall = 0
        self._logical = 0
        self._max_skew_ms = max_skew_ms

    @property
    def node(self) -> NodeId:
        return self._node

    def peek(self) -> HybridTimestamp:
        """The last-issued stamp without advancing (for inspection / the simulator)."""
        return HybridTimestamp(self._wall, self._logical, self._node)

    def now(self) -> HybridTimestamp:
        """Mint a stamp for a *local* event observed at the current physical time."""
        pt = self._physical()
        if pt > self._wall:
            self._wall = pt
            self._logical = 0
        else:
            self._logical += 1
        return HybridTimestamp(self._wall, self._logical, self._node)

    def send(self) -> HybridTimestamp:
        """Mint the stamp to attach to an outgoing message (identical to ``now``)."""
        return self.now()

    def recv(self, remote: HybridTimestamp) -> HybridTimestamp:
        """Fold a received ``remote`` stamp into local time and mint a new local stamp.

        The new wall is the max of local physical time, our wall, and the
        remote wall (subject to the skew clamp); the logical counter is chosen
        so the result strictly dominates *both* our previous stamp and the
        remote stamp — the HLC receive rule, which is what makes causality
        (`a -> b => ts(a) < ts(b)`) hold across nodes.
        """
        pt = self._physical()
        remote_wall = remote.wall_ms
        # Clamp a remote that is implausibly far ahead of our physical clock.
        if remote_wall - pt > self._max_skew_ms:
            remote_wall = pt
        new_wall = max(pt, self._wall, remote_wall)
        if new_wall == self._wall and new_wall == remote_wall:
            new_logical = max(self._logical, remote.logical) + 1
        elif new_wall == self._wall:
            new_logical = self._logical + 1
        elif new_wall == remote_wall:
            new_logical = remote.logical + 1
        else:
            new_logical = 0
        self._wall = new_wall
        self._logical = new_logical
        return HybridTimestamp(self._wall, self._logical, self._node)


class ManualClock:
    """A deterministic, injectable physical clock for the simulator and tests.

    Holds an epoch-millisecond value you advance explicitly. Usable directly as
    a :data:`PhysicalClock` because it is callable. Skew can be modelled by
    handing different nodes ``ManualClock`` instances at different offsets.
    """

    __slots__ = ("_now_ms",)

    def __init__(self, start_ms: int = 0) -> None:
        self._now_ms = start_ms

    def __call__(self) -> int:
        return self._now_ms

    @property
    def now_ms(self) -> int:
        return self._now_ms

    def advance(self, delta_ms: int) -> int:
        """Move time forward by ``delta_ms`` (must be non-negative); return new time."""
        if delta_ms < 0:
            raise ValueError("ManualClock cannot move backwards")
        self._now_ms += delta_ms
        return self._now_ms

    def set(self, now_ms: int) -> None:
        """Jump to an absolute time (may move backwards - models a clock reset)."""
        self._now_ms = now_ms
