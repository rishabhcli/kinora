"""The view/table dependency graph.

Views depend on source tables (and potentially on other views — a view of a
view). To maintain consistency the engine must:

* know which views a given table's change can affect (routing), and
* refresh dependent views in **topological order** so a view never reads a
  stale upstream view within the same change batch.

:class:`DependencyGraph` is a tiny DAG over node names (tables are sources,
views are derived nodes). It detects cycles (an illegal view definition) and
yields a stable topological order plus the transitive set of views dirtied by a
set of changed tables.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable


class DependencyCycleError(ValueError):
    """Raised when the declared view dependencies contain a cycle."""


class DependencyGraph:
    """A DAG of ``dependent -> {dependencies}`` over table/view node names."""

    def __init__(self) -> None:
        # node -> set of nodes it depends ON (its inputs)
        self._deps: dict[str, set[str]] = defaultdict(set)
        # node -> set of nodes that depend on it (its consumers) — reverse index
        self._dependents: dict[str, set[str]] = defaultdict(set)
        self._views: set[str] = set()

    def add_view(self, view: str, depends_on: Iterable[str]) -> None:
        """Declare ``view`` as depending on the given table/view nodes."""
        self._views.add(view)
        for dep in depends_on:
            self._deps[view].add(dep)
            self._dependents[dep].add(view)
        self._check_acyclic()

    @property
    def views(self) -> set[str]:
        return set(self._views)

    def dependencies_of(self, node: str) -> set[str]:
        return set(self._deps.get(node, set()))

    def dependents_of(self, node: str) -> set[str]:
        return set(self._dependents.get(node, set()))

    def dirty_views(self, changed_tables: Iterable[str]) -> set[str]:
        """All views transitively affected by changes to ``changed_tables``."""
        dirty: set[str] = set()
        queue: deque[str] = deque(changed_tables)
        while queue:
            node = queue.popleft()
            for consumer in self._dependents.get(node, set()):
                if consumer not in dirty:
                    dirty.add(consumer)
                    queue.append(consumer)  # a view-of-a-view propagates further
        return dirty

    def topological_order(self, nodes: Iterable[str] | None = None) -> list[str]:
        """A dependency-respecting order over ``nodes`` (default: all views).

        Inputs come before the views that consume them. Used so a batch refresh
        updates upstream views first.
        """
        targets = set(nodes) if nodes is not None else set(self._views)
        # Restrict the graph to targets + their relevant edges.
        indeg: dict[str, int] = dict.fromkeys(targets, 0)
        for n in targets:
            for dep in self._deps.get(n, set()):
                if dep in targets:
                    indeg[n] += 1
        ready: deque[str] = deque(sorted(n for n, d in indeg.items() if d == 0))
        order: list[str] = []
        while ready:
            node = ready.popleft()
            order.append(node)
            for consumer in sorted(self._dependents.get(node, set())):
                if consumer in indeg:
                    indeg[consumer] -= 1
                    if indeg[consumer] == 0:
                        ready.append(consumer)
        if len(order) != len(targets):
            raise DependencyCycleError("cycle detected among view dependencies")
        return order

    def _check_acyclic(self) -> None:
        # Full topo over every known node; raises on cycle.
        all_nodes = set(self._deps) | set(self._dependents) | self._views
        self.topological_order(all_nodes)


__all__ = ["DependencyCycleError", "DependencyGraph"]
