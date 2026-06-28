"""A qualitative temporal constraint network over canon facts (§8.5).

The contradiction detector (``contradiction.py``) finds *direct* clashes — two
facts that overlap and disagree. But a long adaptation accumulates *implied*
contradictions: the canon may say event A is BEFORE B, B is BEFORE C, and C is
BEFORE A — each pairwise constraint is fine, yet together they are unsatisfiable
(a temporal cycle). Catching that needs constraint propagation.

This module builds an :class:`AllenNetwork`: nodes are named intervals (a fact's
lifetime, or an abstract event the author asserted), edges carry a
:class:`~.composition.RelationSet`. :meth:`path_consistency` runs the classic
path-consistency algorithm (van Beek) — repeatedly tightening each edge ``i→j``
by intersecting it with ``compose(i→k, k→j)`` until a fixpoint. An edge that
collapses to the empty set proves the network is **inconsistent**, and the engine
emits the cycle as a proof trace.

Pure: no I/O. Seeded from a :class:`~.timeline.CanonTimeline` (each fact's
interval is read off; pairwise relations are computed by
:meth:`BeatInterval.relate`) or from hand-asserted qualitative constraints.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .composition import ALL_RELATIONS, RelationSet, compose, converse
from .intervals import Allen, BeatInterval
from .proof import ProofStep, ProofTrace, Rule
from .timeline import CanonTimeline


@dataclass
class AllenNetwork:
    """A binary constraint network of named intervals with Allen-relation edges.

    ``edges[(i, j)]`` is the set of relations still possible for ``i`` relative to
    ``j``; a missing edge is the universal relation. Invariants: ``edges`` is kept
    converse-symmetric (``edges[(j, i)] == converse(edges[(i, j))]``) and every
    node has the reflexive ``EQUALS`` self-edge.
    """

    nodes: list[str] = field(default_factory=list)
    edges: dict[tuple[str, str], RelationSet] = field(default_factory=dict)
    #: Records the triangle that collapsed an edge (for the proof trace).
    _justification: dict[tuple[str, str], str] = field(default_factory=dict, repr=False)

    def add_node(self, name: str) -> None:
        if name not in self.nodes:
            self.nodes.append(name)
            self.edges[(name, name)] = frozenset({Allen.EQUALS})

    def constrain(self, i: str, j: str, relations: RelationSet) -> None:
        """Assert ``i`` stands in one of ``relations`` to ``j`` (and the converse).

        Intersects with any existing constraint, so repeated asserts tighten.
        """
        self.add_node(i)
        self.add_node(j)
        current = self.edges.get((i, j), ALL_RELATIONS)
        tightened = current & relations
        self.edges[(i, j)] = tightened
        self.edges[(j, i)] = converse(tightened)

    def relation(self, i: str, j: str) -> RelationSet:
        """The current relation set for ``i`` relative to ``j`` (universal default)."""
        if i == j:
            return frozenset({Allen.EQUALS})
        return self.edges.get((i, j), ALL_RELATIONS)

    @classmethod
    def from_intervals(cls, intervals: dict[str, BeatInterval]) -> AllenNetwork:
        """Build a fully-determined network from concrete intervals.

        Every pair's relation is read off by :meth:`BeatInterval.relate`, so the
        network starts singleton-tight; :meth:`path_consistency` then verifies it
        is self-consistent (it always is for real intervals) and can be combined
        with looser hand-asserted constraints to detect a clash.
        """
        net = cls()
        names = list(intervals)
        for name in names:
            net.add_node(name)
        for i in names:
            for j in names:
                if i == j:
                    continue
                net.edges[(i, j)] = frozenset({intervals[i].relate(intervals[j])})
        return net

    @classmethod
    def from_timeline_subject(cls, timeline: CanonTimeline, subject: str) -> AllenNetwork:
        """A network of all of ``subject``'s facts, keyed by fact id."""
        intervals = {
            (f.fact_id or f"{f.predicate}:{f.object}"): f.interval
            for f in timeline.facts_about(subject)
        }
        return cls.from_intervals(intervals)

    def path_consistency(self) -> ConsistencyResult:
        """Run path-consistency to a fixpoint; report consistency + any collapse.

        Tightens each edge ``i→j`` by ``∩ compose(i→k, k→j)`` over all ``k`` until
        nothing changes. If any edge becomes empty the network is inconsistent and
        the result carries the offending triangle as a proof trace.
        """
        # Worklist of (i, j) pairs to (re)process; seed with all directed pairs.
        queue: list[tuple[str, str]] = [
            (i, j) for i in self.nodes for j in self.nodes if i != j
        ]
        while queue:
            i, j = queue.pop()
            for k in self.nodes:
                if k in (i, j):
                    continue
                # Tighten i→k via i→j ∘ j→k, and i→j via i→k ∘ k→j (both directions).
                changed_pair = self._tighten(i, k, j)
                if changed_pair is not None:
                    if changed_pair.empty:
                        return changed_pair
                    queue.extend(self._neighbours(i, k))
        return ConsistencyResult(consistent=True)

    def _tighten(self, i: str, k: str, via: str) -> ConsistencyResult | None:
        """Tighten edge ``i→k`` using the path through ``via``; None if unchanged."""
        composed = compose(self.relation(i, via), self.relation(via, k))
        current = self.relation(i, k)
        new = current & composed
        if new == current:
            return None
        self.edges[(i, k)] = new
        self.edges[(k, i)] = converse(new)
        self._justification[(i, k)] = via
        if not new:
            trace = self._collapse_trace(i, k, via)
            return ConsistencyResult(consistent=False, empty=True, trace=trace)
        return ConsistencyResult(consistent=True, empty=False)

    def _neighbours(self, i: str, k: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for n in self.nodes:
            if n not in (i, k):
                out.append((i, n))
                out.append((n, k))
        return out

    def _collapse_trace(self, i: str, k: str, via: str) -> ProofTrace:
        r_iv = _fmt(self.relation(i, via))
        r_vk = _fmt(self.relation(via, k))
        return ProofTrace(
            summary=f"temporal cycle: {i}, {via}, {k} cannot be jointly ordered",
            steps=(
                ProofStep(
                    rule=Rule.TEMPORAL_RELATION,
                    premises=(f"{i} {r_iv} {via}", f"{via} {r_vk} {k}"),
                    conclusion=(
                        f"composition forces {i}→{k} ∈ "
                        f"{_fmt(compose(self.relation(i, via), self.relation(via, k)))}"
                    ),
                ),
                ProofStep(
                    rule=Rule.TEMPORAL_RELATION,
                    premises=(f"but the canon also fixes {i}→{k} elsewhere",),
                    conclusion=(
                        "INCONSISTENT: the intersection is empty — no ordering "
                        f"of {i}, {via}, {k} satisfies all constraints"
                    ),
                ),
            ),
            contradiction=True,
        )


@dataclass(frozen=True, slots=True)
class ConsistencyResult:
    """The verdict of path-consistency: consistent, or an empty-edge collapse."""

    consistent: bool
    empty: bool = False
    trace: ProofTrace | None = None


def _fmt(rel_set: RelationSet) -> str:
    return "{" + ",".join(sorted(r.value for r in rel_set)) + "}"


__all__ = ["AllenNetwork", "ConsistencyResult"]
