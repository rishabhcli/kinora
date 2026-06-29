"""Feature lineage — a queryable graph of how features come to be.

Lineage answers "where did this feature column come from, and what breaks if I
change it?". The graph nodes are *sources*, *feature views* (versioned),
*on-demand views*, and *feature services*; edges are ``produces`` (source →
view), ``derives`` (view → on-demand view), and ``consumes`` (view → service).
It is built purely from a :class:`~app.lakehouse.features.registry.FeatureRegistry`
snapshot, so it always reflects the registered definitions.

Two read paths matter operationally:

* **upstream(column)** — given a ``view__feature`` output column, the chain of
  views/sources that feed it (debugging a bad value, audit).
* **downstream(view)** — given a feature view, the on-demand views and feature
  services that would be affected by changing/retiring it (blast radius before a
  schema change).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .registry import FeatureRegistry


@dataclass(frozen=True, slots=True)
class LineageNode:
    id: str
    kind: str  # "source" | "feature_view" | "on_demand_view" | "feature_service"
    label: str


@dataclass(frozen=True, slots=True)
class LineageEdge:
    src: str
    dst: str
    relation: str  # "produces" | "derives" | "consumes"


@dataclass(frozen=True, slots=True)
class LineageGraph:
    nodes: tuple[LineageNode, ...]
    edges: tuple[LineageEdge, ...]
    # Adjacency caches keyed by node id (forward + reverse), built in __post_init__.
    _fwd: dict[str, list[str]] = field(default_factory=dict, compare=False)
    _rev: dict[str, list[str]] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        for e in self.edges:
            self._fwd.setdefault(e.src, []).append(e.dst)
            self._rev.setdefault(e.dst, []).append(e.src)

    def node(self, node_id: str) -> LineageNode | None:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def _walk(self, start: str, adjacency: dict[str, list[str]]) -> list[str]:
        seen: list[str] = []
        seen_set: set[str] = set()
        stack = list(adjacency.get(start, ()))
        while stack:
            cur = stack.pop()
            if cur in seen_set:
                continue
            seen_set.add(cur)
            seen.append(cur)
            stack.extend(adjacency.get(cur, ()))
        return seen

    def upstream(self, node_id: str) -> list[str]:
        """All transitive ancestors of ``node_id`` (sources & views that feed it)."""
        return self._walk(node_id, self._rev)

    def downstream(self, node_id: str) -> list[str]:
        """All transitive descendants of ``node_id`` (the change blast radius)."""
        return self._walk(node_id, self._fwd)


def _view_node_id(name: str, version: int) -> str:
    return f"view:{name}@{version}"


def build_lineage(registry: FeatureRegistry) -> LineageGraph:
    """Build the lineage graph from a registry snapshot."""
    nodes: list[LineageNode] = []
    edges: list[LineageEdge] = []
    seen_nodes: set[str] = set()

    def add_node(node: LineageNode) -> None:
        if node.id not in seen_nodes:
            seen_nodes.add(node.id)
            nodes.append(node)

    view_id_by_name: dict[str, str] = {}
    for view in registry.list_feature_views():
        vid = _view_node_id(view.name, view.version)
        view_id_by_name[view.name] = vid
        add_node(LineageNode(id=vid, kind="feature_view", label=f"{view.name} v{view.version}"))
        src_id = f"source:{view.source.name}"
        add_node(LineageNode(id=src_id, kind="source", label=view.source.name))
        edges.append(LineageEdge(src=src_id, dst=vid, relation="produces"))

    for odv in registry.list_on_demand_views():
        oid = f"on_demand:{odv.name}"
        add_node(LineageNode(id=oid, kind="on_demand_view", label=odv.name))
        for src_view in odv.source_views:
            if src_view in view_id_by_name:
                edges.append(
                    LineageEdge(src=view_id_by_name[src_view], dst=oid, relation="derives")
                )

    for service in registry.list_feature_services():
        sid = f"service:{service.name}"
        add_node(LineageNode(id=sid, kind="feature_service", label=service.name))
        for ref in service.refs():
            view = registry.get_feature_view(ref.view, version=ref.version)
            vid = _view_node_id(view.name, view.version)
            edges.append(LineageEdge(src=vid, dst=sid, relation="consumes"))

    return LineageGraph(nodes=tuple(nodes), edges=tuple(edges))


def column_view(column: str) -> str | None:
    """Extract the view name from a ``view__feature`` output column."""
    view, sep, _ = column.partition("__")
    return view if sep else None


def affected_services(registry: FeatureRegistry, view_name: str) -> Iterable[str]:
    """Feature services that consume a given feature view (the change blast radius)."""
    graph = build_lineage(registry)
    versions = registry.feature_view_versions(view_name)
    affected: set[str] = set()
    for version in versions:
        for node_id in graph.downstream(_view_node_id(view_name, version)):
            node = graph.node(node_id)
            if node is not None and node.kind == "feature_service":
                affected.add(node.label)
    return sorted(affected)


__all__ = [
    "LineageEdge",
    "LineageGraph",
    "LineageNode",
    "affected_services",
    "build_lineage",
    "column_view",
]
