"""Version vectors: per-node causal frontiers for the replication protocol.

A :class:`VersionVector` maps each :class:`~app.distributed.replication.clock.NodeId`
to the highest contiguous sequence number this replica has *durably applied* from
that node's replication log. It is the compact summary every replica exchanges so
a peer can compute "what have you not seen?" in one round trip — the gossip /
anti-entropy digest, and the causal-readiness check for delivery ordering.

This is **distinct** from the version vector in :mod:`app.memory.crdt`, which is a
grow-only edit tally over canon actors. Here the vector is a *log frontier* over
replication nodes with the partial order, dominance, and set-difference operations
the active-active protocol needs:

* :meth:`VersionVector.dominates` — causal "I have seen everything you have".
* :meth:`VersionVector.concurrent_with` — neither dominates (a real divergence).
* :meth:`VersionVector.merge` — the pointwise max (a CRDT join: commutative,
  associative, idempotent), used when reconciling two frontiers.
* :meth:`VersionVector.missing_ranges` — the per-node ``(after_seq)`` gaps a peer
  must ship us to catch us up to its frontier.

Pure, hashable-by-value, dependency-free.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.distributed.replication.clock import NodeId


@dataclass(frozen=True, slots=True)
class VersionVector:
    """An immutable per-node log frontier.

    ``entries`` maps a node to the highest *contiguous* sequence number applied
    from it. A node absent from the map is implicitly at sequence ``0`` (nothing
    seen). Construct via :meth:`empty` / :meth:`of`; never mutate — every
    operation returns a fresh vector.
    """

    entries: Mapping[NodeId, int]

    @classmethod
    def empty(cls) -> VersionVector:
        return cls({})

    @classmethod
    def of(cls, entries: Mapping[NodeId, int]) -> VersionVector:
        """Build from a mapping, dropping any non-positive (==0) entries."""
        return cls({n: s for n, s in entries.items() if s > 0})

    def get(self, node: NodeId) -> int:
        """The highest contiguous sequence applied from ``node`` (0 if unseen)."""
        return self.entries.get(node, 0)

    def nodes(self) -> frozenset[NodeId]:
        return frozenset(self.entries)

    def advanced(self, node: NodeId, seq: int) -> VersionVector:
        """Return a vector with ``node`` bumped to ``max(current, seq)``."""
        if seq <= self.get(node):
            return self
        updated = dict(self.entries)
        updated[node] = seq
        return VersionVector(updated)

    def includes(self, node: NodeId, seq: int) -> bool:
        """True iff we have already applied ``(node, seq)`` (it is at or below frontier)."""
        return seq <= self.get(node)

    def dominates(self, other: VersionVector) -> bool:
        """True iff this frontier causally includes everything in ``other``.

        ``self >= other`` pointwise. ``a.dominates(b)`` means "a has seen at
        least everything b has", so b's updates are all safe to consider applied
        from a's perspective.
        """
        return all(self.get(node) >= other.get(node) for node in self.nodes() | other.nodes())

    def strictly_dominates(self, other: VersionVector) -> bool:
        """:meth:`dominates` and the two are not equal (a strict causal lead)."""
        return self.dominates(other) and self != other

    def concurrent_with(self, other: VersionVector) -> bool:
        """True iff neither dominates the other — a genuine concurrent divergence."""
        return not self.dominates(other) and not other.dominates(self)

    def merge(self, other: VersionVector) -> VersionVector:
        """Pointwise max join of two frontiers (the CRDT least-upper-bound)."""
        merged = dict(self.entries)
        for node, seq in other.entries.items():
            if seq > merged.get(node, 0):
                merged[node] = seq
        return VersionVector(merged)

    def missing_ranges(self, ahead: VersionVector) -> dict[NodeId, int]:
        """Per-node ``after_seq`` cursors describing what ``ahead`` has and we don't.

        For each node where ``ahead`` is in front, the value is *our* frontier:
        the peer should ship every record from that node with ``seq > value``.
        Nodes where we are equal or ahead are omitted (nothing to fetch).
        """
        gaps: dict[NodeId, int] = {}
        for node in ahead.nodes() | self.nodes():
            mine = self.get(node)
            theirs = ahead.get(node)
            if theirs > mine:
                gaps[node] = mine
        return gaps

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VersionVector):
            return NotImplemented
        nodes = self.nodes() | other.nodes()
        return all(self.get(n) == other.get(n) for n in nodes)

    def __hash__(self) -> int:
        return hash(frozenset((n, s) for n, s in self.entries.items() if s > 0))

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        body = ", ".join(f"{n}:{s}" for n, s in sorted(self.entries.items()))
        return f"VersionVector({{{body}}})"


def join_all(vectors: Iterable[VersionVector]) -> VersionVector:
    """Merge an arbitrary collection of frontiers into their least upper bound."""
    acc = VersionVector.empty()
    for v in vectors:
        acc = acc.merge(v)
    return acc
