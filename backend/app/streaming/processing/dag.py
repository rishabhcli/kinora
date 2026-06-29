"""The logical dataflow graph — an immutable DAG of operator nodes.

A :class:`DataStream` transformation builds up a :class:`StreamGraph`: a list of
:class:`StreamNode`\\ s linked by parent ids. The graph is *logical* — it names
operators and their wiring but holds no runtime state; the
:class:`~app.streaming.processing.runtime.JobExecutor` instantiates each node's
operator, allocates its keyed-state backend, and pushes records through in
topological order.

Keeping the graph separate from execution gives the same benefit Flink's
StreamGraph does: the topology is inspectable and serializable (great for the
``agent_activity`` feed / a topology view), and a single graph can be executed by
different runtimes (the deterministic test driver vs. a future async runner).
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.streaming.processing.operators import Operator

#: Monotonic node-id source (per process). Ids are stable within one build.
_node_ids = itertools.count(1)


def _next_node_id(prefix: str) -> str:
    return f"{prefix}-{next(_node_ids)}"


@dataclass(slots=True)
class StreamNode:
    """One operator in the logical graph.

    ``operator_factory`` builds a fresh operator instance at execution time (so
    one graph can be executed repeatedly with clean state). ``parents`` are the
    node ids feeding this node; ``keyed`` marks nodes the runtime must key-scope.
    """

    node_id: str
    name: str
    operator_factory: object  # Callable[[], Operator]; loose to avoid a cycle
    parents: list[str] = field(default_factory=list)
    keyed: bool = False
    parallelism: int = 1

    def build(self) -> Operator:
        factory = self.operator_factory
        assert callable(factory)
        return factory()


@dataclass(slots=True)
class StreamGraph:
    """The immutable logical topology: nodes keyed by id, in insertion order."""

    nodes: dict[str, StreamNode] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    sinks: list[str] = field(default_factory=list)

    def add(self, node: StreamNode, *, is_source: bool = False) -> StreamNode:
        self.nodes[node.node_id] = node
        if is_source:
            self.sources.append(node.node_id)
        return node

    def children_of(self, node_id: str) -> list[str]:
        return [n.node_id for n in self.nodes.values() if node_id in n.parents]

    def topological_order(self) -> list[StreamNode]:
        """Kahn's algorithm — sources first, each node after all its parents.

        Raises on a cycle (a DAG must be acyclic). Order is deterministic:
        ties break by node insertion order.
        """

        indeg: dict[str, int] = dict.fromkeys(self.nodes, 0)
        children: dict[str, list[str]] = defaultdict(list)
        for nid, node in self.nodes.items():
            for parent in node.parents:
                indeg[nid] += 1
                children[parent].append(nid)

        ready = [nid for nid in self.nodes if indeg[nid] == 0]
        order: list[StreamNode] = []
        while ready:
            nid = ready.pop(0)
            order.append(self.nodes[nid])
            for child in children[nid]:
                indeg[child] -= 1
                if indeg[child] == 0:
                    ready.append(child)
        if len(order) != len(self.nodes):
            raise ValueError("stream graph contains a cycle")
        return order

    def describe(self) -> list[str]:
        """A human-readable topology listing for the activity feed / debugging."""

        lines: list[str] = []
        for node in self.topological_order():
            parents = ",".join(node.parents) or "<source>"
            flag = " [keyed]" if node.keyed else ""
            lines.append(f"{node.node_id} {node.name}{flag}  <- {parents}")
        return lines


def new_node(
    name: str,
    operator_factory: object,
    *,
    parents: list[str] | None = None,
    keyed: bool = False,
    prefix: str = "op",
) -> StreamNode:
    """Construct a :class:`StreamNode` with a fresh id."""

    return StreamNode(
        node_id=_next_node_id(prefix),
        name=name,
        operator_factory=operator_factory,
        parents=parents or [],
        keyed=keyed,
    )
