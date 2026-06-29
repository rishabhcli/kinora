"""Dependency resolution — version-range satisfaction + topological ordering.

Before a plugin can be enabled, every (non-optional) dependency it declares must
be **present, enabled, and version-compatible**, and the graph must be acyclic.
This module is pure: it takes the set of *available* plugin versions and a target
to resolve, and returns either a valid install/enable order or a structured
:class:`DependencyResolutionError` explaining the first failure.

The algorithm:

1. **Candidate selection** — for each dependency ``(id, range)`` pick the highest
   available version satisfying the range (latest-wins, deterministic).
2. **Closure** — recursively resolve the candidates' own dependencies, detecting
   missing nodes and version conflicts (a node pinned to two disjoint ranges).
3. **Cycle detection + topological sort** — produce an order where dependencies
   precede dependents (Kahn's algorithm); a cycle raises.

Optional dependencies that cannot be satisfied are *skipped* (not an error); a
missing required dependency, an unsatisfiable range, or a conflict raises.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import NoReturn

from app.platform.plugins.errors import DependencyResolutionError
from app.platform.plugins.manifest import Dependency, PluginManifest
from app.platform.plugins.version import Version


@dataclass(frozen=True, slots=True)
class AvailablePlugin:
    """A plugin version known to the resolver (present in the registry)."""

    id: str
    version: Version
    dependencies: tuple[Dependency, ...] = ()
    enabled: bool = True


@dataclass(slots=True)
class ResolutionResult:
    """A successful resolution: the chosen versions + a valid enable order."""

    #: plugin_id -> chosen Version (the target plus its transitive deps).
    chosen: dict[str, Version] = field(default_factory=dict)
    #: An order where every dependency appears before the plugins that need it.
    order: list[str] = field(default_factory=list)
    #: Optional dependencies that were declared but not satisfiable (skipped).
    skipped_optional: list[str] = field(default_factory=list)


class DependencyResolver:
    """Resolves a target plugin's dependency closure against available versions."""

    def __init__(self, available: Iterable[AvailablePlugin]) -> None:
        # id -> {version: AvailablePlugin}; multiple versions of one id may coexist.
        self._index: dict[str, dict[Version, AvailablePlugin]] = {}
        for plugin in available:
            self._index.setdefault(plugin.id, {})[plugin.version] = plugin

    # ------------------------------------------------------------------ #

    def resolve(self, manifest: PluginManifest) -> ResolutionResult:
        """Resolve ``manifest``'s full dependency closure (raises on failure)."""
        result = ResolutionResult()
        result.chosen[manifest.id] = manifest.version
        # The dependency graph among resolved nodes (id -> set of dep ids).
        edges: dict[str, set[str]] = {manifest.id: set()}
        self._resolve_deps(manifest.id, manifest.dependencies, result, edges)
        result.order = _topological_sort(edges)
        return result

    def _resolve_deps(
        self,
        parent: str,
        deps: tuple[Dependency, ...],
        result: ResolutionResult,
        edges: dict[str, set[str]],
    ) -> None:
        for dep in deps:
            candidate = self._best_candidate(dep)
            if candidate is None:
                if dep.optional:
                    result.skipped_optional.append(dep.plugin_id)
                    continue
                self._raise_unsatisfied(dep)

            # Version-conflict check: a node already chosen at an incompatible
            # version cannot also satisfy this range.
            already = result.chosen.get(dep.plugin_id)
            if already is not None and not dep.range.matches(already):
                raise DependencyResolutionError(
                    f"version conflict for {dep.plugin_id!r}: already pinned to "
                    f"{already} but {parent!r} needs {dep.range}"
                )

            edges.setdefault(parent, set()).add(dep.plugin_id)
            if dep.plugin_id in result.chosen:
                # Already resolved (and compatible) — avoid infinite recursion.
                continue
            result.chosen[dep.plugin_id] = candidate.version
            edges.setdefault(dep.plugin_id, set())
            self._resolve_deps(dep.plugin_id, candidate.dependencies, result, edges)

    def _best_candidate(self, dep: Dependency) -> AvailablePlugin | None:
        """Highest enabled version of ``dep.plugin_id`` satisfying its range."""
        versions = self._index.get(dep.plugin_id)
        if not versions:
            return None
        matching = [p for v, p in versions.items() if p.enabled and dep.range.matches(v)]
        if not matching:
            return None
        return max(matching, key=lambda p: p.version)

    def _raise_unsatisfied(self, dep: Dependency) -> NoReturn:
        versions = self._index.get(dep.plugin_id)
        if not versions:
            raise DependencyResolutionError(
                f"required dependency {dep.plugin_id!r} is not installed"
            )
        present = ", ".join(str(v) for v in sorted(versions))
        enabled_present = any(p.enabled for p in versions.values())
        if not enabled_present:
            raise DependencyResolutionError(
                f"dependency {dep.plugin_id!r} is installed but disabled (have {present})"
            )
        raise DependencyResolutionError(
            f"no version of {dep.plugin_id!r} satisfies {dep.range} (have {present})"
        )


def _topological_sort(edges: Mapping[str, set[str]]) -> list[str]:
    """Kahn's algorithm: return deps-before-dependents order (raises on cycle).

    ``edges[a]`` is the set of ids ``a`` depends on. The returned order places
    every dependency before the node that needs it. Ties broken alphabetically
    for determinism.
    """
    # Build indegree over the reverse direction: a node is "ready" when all its
    # dependencies are emitted. indegree[node] = number of its unmet deps.
    nodes = set(edges)
    for deps in edges.values():
        nodes |= deps
    indegree = {n: len(edges.get(n, set())) for n in nodes}
    ready = sorted(n for n, d in indegree.items() if d == 0)
    order: list[str] = []
    # Reverse adjacency: dependency -> dependents that wait on it.
    dependents: dict[str, list[str]] = {n: [] for n in nodes}
    for node, deps in edges.items():
        for d in deps:
            dependents[d].append(node)

    while ready:
        node = ready.pop(0)
        order.append(node)
        for dependent in sorted(dependents.get(node, ())):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                # Insert maintaining sorted order for determinism.
                ready.append(dependent)
                ready.sort()

    if len(order) != len(nodes):
        cyclic = sorted(n for n in nodes if n not in order)
        raise DependencyResolutionError(f"dependency cycle detected among: {', '.join(cyclic)}")
    return order


__all__ = [
    "AvailablePlugin",
    "DependencyResolver",
    "ResolutionResult",
    "_topological_sort",
]
