"""Graph reasoning over a canon snapshot (kinora.md §8.1, §9.5).

The canon is a knowledge graph: entities are nodes, continuity facts are typed edges
(``subject --predicate--> object``). At query time the agents need cheap structural
answers over the *active* slice — who is connected to whom, can A reach B, is there a
contradiction the Critic's timeline check (§9.5) should flag?

This module is **pure**: it operates on already-resolved fact/entity lists (the output of a
bitemporal ``as_of`` read or a :class:`~app.memory.interfaces.CanonSlice`), never the DB. So
the reasoning is fully unit-testable offline, and the same functions work over any snapshot
— main, a fork, or a past tx-belief.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.memory.contracts import BitemporalFact


@dataclass(frozen=True, slots=True)
class Edge:
    """A directed, typed relationship edge between two canon nodes."""

    subject: str
    predicate: str
    object: str
    fact_key: str = ""


@dataclass(slots=True)
class CanonGraph:
    """An adjacency-list view of a canon snapshot for structural reasoning.

    Nodes are entity keys *and* fact-object values (so ``hero --possesses--> sword`` makes
    ``sword`` reachable even if it isn't a registered entity). Edges are the active facts.
    """

    edges: list[Edge] = field(default_factory=list)
    _out: dict[str, list[Edge]] = field(default_factory=lambda: defaultdict(list))
    _in: dict[str, list[Edge]] = field(default_factory=lambda: defaultdict(list))

    @classmethod
    def from_facts(cls, facts: Iterable[BitemporalFact]) -> CanonGraph:
        """Build the graph from active bitemporal facts (subject→object edges)."""
        graph = cls()
        for fact in facts:
            graph.add_edge(
                Edge(
                    subject=fact.subject_entity_key,
                    predicate=fact.predicate,
                    object=fact.object_value,
                    fact_key=fact.fact_key,
                )
            )
        return graph

    @classmethod
    def from_triples(cls, triples: Iterable[tuple[str, str, str]]) -> CanonGraph:
        """Build from raw ``(subject, predicate, object)`` triples (test/seed convenience)."""
        graph = cls()
        for subject, predicate, obj in triples:
            graph.add_edge(Edge(subject=subject, predicate=predicate, object=obj))
        return graph

    def add_edge(self, edge: Edge) -> None:
        self.edges.append(edge)
        self._out[edge.subject].append(edge)
        self._in[edge.object].append(edge)

    @property
    def nodes(self) -> set[str]:
        return set(self._out) | set(self._in)

    def out_edges(self, node: str) -> list[Edge]:
        return list(self._out.get(node, ()))

    def in_edges(self, node: str) -> list[Edge]:
        return list(self._in.get(node, ()))

    def neighbors(self, node: str) -> set[str]:
        """All nodes one hop away (either direction)."""
        return {e.object for e in self._out.get(node, ())} | {
            e.subject for e in self._in.get(node, ())
        }

    def reachable(self, start: str, *, max_hops: int | None = None) -> set[str]:
        """Every node reachable from ``start`` following edge direction (BFS)."""
        seen: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        while queue:
            node, depth = queue.popleft()
            if max_hops is not None and depth >= max_hops:
                continue
            for edge in self._out.get(node, ()):
                if edge.object not in seen:
                    seen.add(edge.object)
                    queue.append((edge.object, depth + 1))
        return seen

    def shortest_path(self, start: str, goal: str) -> list[Edge] | None:
        """The shortest directed edge-path ``start → … → goal`` (BFS), or None."""
        if start == goal:
            return []
        prev: dict[str, Edge] = {}
        seen = {start}
        queue: deque[str] = deque([start])
        while queue:
            node = queue.popleft()
            for edge in self._out.get(node, ()):
                nxt = edge.object
                if nxt in seen:
                    continue
                seen.add(nxt)
                prev[nxt] = edge
                if nxt == goal:
                    return _reconstruct(prev, goal)
                queue.append(nxt)
        return None

    def neighborhood(self, node: str, *, hops: int = 1) -> CanonGraph:
        """A sub-graph of every edge within ``hops`` of ``node`` (either direction).

        This is the structural analogue of ``canon.query``'s "only what this beat needs":
        give an agent the entity and its immediate relationships, not the whole graph.
        """
        keep: set[str] = {node}
        frontier = {node}
        for _ in range(hops):
            nxt: set[str] = set()
            for current in frontier:
                nxt |= self.neighbors(current)
            keep |= nxt
            frontier = nxt
        sub = CanonGraph()
        for edge in self.edges:
            if edge.subject in keep and edge.object in keep:
                sub.add_edge(edge)
        return sub


@dataclass(frozen=True, slots=True)
class Contradiction:
    """Two active facts that cannot co-hold (the Critic's §9.5 timeline check input)."""

    subject: str
    predicate: str
    object_a: str
    object_b: str
    fact_key_a: str
    fact_key_b: str
    reason: str


#: Predicates whose object is *functional* — a subject can hold at most one at a time. Two
#: active facts on a functional predicate with different objects are a contradiction (e.g. a
#: character cannot be in two locations at once, cannot be both alive and dead).
FUNCTIONAL_PREDICATES: frozenset[str] = frozenset(
    {"located_at", "location", "status", "alive", "age", "wears", "holds_title", "possesses_unique"}
)


def find_contradictions(
    facts: Iterable[BitemporalFact],
    *,
    functional_predicates: frozenset[str] = FUNCTIONAL_PREDICATES,
    mutually_exclusive: Iterable[tuple[str, str]] = (),
) -> list[Contradiction]:
    """Detect facts that cannot simultaneously hold over a snapshot (§9.5 timeline).

    Two kinds:
      * **functional clash** — same (subject, predicate) on a functional predicate but
        different objects (a subject can't be in two places at once);
      * **mutually-exclusive predicates** — a subject holds two predicates declared
        incompatible (e.g. ``("alive", "dead")``).
    """
    facts = list(facts)
    contradictions: list[Contradiction] = []

    by_subject_pred: dict[tuple[str, str], list[BitemporalFact]] = defaultdict(list)
    by_subject_pred_set: dict[str, dict[str, BitemporalFact]] = defaultdict(dict)
    for fact in facts:
        by_subject_pred[(fact.subject_entity_key, fact.predicate)].append(fact)
        by_subject_pred_set[fact.subject_entity_key][fact.predicate] = fact

    for (subject, predicate), group in by_subject_pred.items():
        if predicate not in functional_predicates:
            continue
        objects = {f.object_value: f for f in group}
        if len(objects) > 1:
            items = list(objects.values())
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    contradictions.append(
                        Contradiction(
                            subject=subject,
                            predicate=predicate,
                            object_a=items[i].object_value,
                            object_b=items[j].object_value,
                            fact_key_a=items[i].fact_key,
                            fact_key_b=items[j].fact_key,
                            reason=f"functional predicate '{predicate}' has conflicting values",
                        )
                    )

    exclusions = {frozenset(pair) for pair in mutually_exclusive}
    for subject, preds in by_subject_pred_set.items():
        present = set(preds)
        for pair in exclusions:
            if pair <= present:
                a, b = tuple(pair)
                contradictions.append(
                    Contradiction(
                        subject=subject,
                        predicate=f"{a}|{b}",
                        object_a=preds[a].object_value,
                        object_b=preds[b].object_value,
                        fact_key_a=preds[a].fact_key,
                        fact_key_b=preds[b].fact_key,
                        reason=f"mutually-exclusive predicates both active: {a} & {b}",
                    )
                )
    return contradictions


def _reconstruct(prev: dict[str, Edge], goal: str) -> list[Edge]:
    path: list[Edge] = []
    node = goal
    while node in prev:
        edge = prev[node]
        path.append(edge)
        node = edge.subject
    path.reverse()
    return path


__all__ = [
    "CanonGraph",
    "Contradiction",
    "Edge",
    "FUNCTIONAL_PREDICATES",
    "find_contradictions",
]
