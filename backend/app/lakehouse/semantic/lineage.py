"""Metric lineage — the dependency graph from KPIs down to physical columns.

"Where does this number come from?" is the first question anyone asks of a
metric. Lineage answers it precisely: for any metric it produces the DAG of
intermediate metrics it is built from, the base measures those bottom out in,
and the *physical source columns* (model.source + expression) the measures read.

The graph is derived entirely from the validated
:class:`~app.lakehouse.semantic.registry.SemanticGraph` (no extra annotation), so
it can never drift from the definitions. It is rendered three ways:

* :meth:`upstream` / :meth:`downstream` — transitive dependency / dependent sets;
* :meth:`lineage_of` — a structured :class:`MetricLineage` (metrics + measures +
  columns + models touched), the payload a "metric detail" UI shows;
* :meth:`to_edges` — a flat edge list (``from -> to``, typed) suitable for a
  graph visualisation or an impact-analysis ("what breaks if I drop this
  column?") query.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.lakehouse.semantic.metrics import SimpleMetric, metric_dependencies
from app.lakehouse.semantic.registry import SemanticGraph


class NodeKind:
    METRIC = "metric"
    MEASURE = "measure"
    COLUMN = "column"
    MODEL = "model"


@dataclass(frozen=True, slots=True)
class LineageEdge:
    """A typed directed edge ``source -> target`` in the lineage graph."""

    source: str
    target: str
    source_kind: str
    target_kind: str


@dataclass(frozen=True, slots=True)
class PhysicalColumn:
    """A physical column a metric ultimately reads."""

    model: str
    source: str
    expression: str

    @property
    def key(self) -> str:
        return f"{self.source}.{self.expression}"


@dataclass(frozen=True, slots=True)
class MetricLineage:
    """The full upstream lineage of one metric."""

    metric: str
    upstream_metrics: tuple[str, ...]
    base_measures: tuple[str, ...]
    physical_columns: tuple[PhysicalColumn, ...]
    models: tuple[str, ...]


class LineageGraph:
    """Derives lineage facts from a semantic graph."""

    def __init__(self, graph: SemanticGraph):
        self.graph = graph

    # -- transitive metric dependencies ----------------------------------- #

    def upstream(self, metric: str) -> tuple[str, ...]:
        """Metrics ``metric`` transitively depends on (excludes itself)."""
        out: list[str] = []
        seen: set[str] = set()
        self._walk_up(metric, seen, out)
        return tuple(m for m in out if m != metric)

    def _walk_up(self, name: str, seen: set[str], out: list[str]) -> None:
        if name in seen:
            return
        seen.add(name)
        for dep in metric_dependencies(self.graph.metric(name)):
            self._walk_up(dep, seen, out)
            if dep not in out:
                out.append(dep)
        if name not in out:
            out.append(name)

    def downstream(self, metric: str) -> tuple[str, ...]:
        """Metrics that (transitively) depend on ``metric`` — impact analysis."""
        dependents: dict[str, set[str]] = {n: set() for n in self.graph.metrics}
        for name in self.graph.metrics:
            for dep in metric_dependencies(self.graph.metric(name)):
                dependents[dep].add(name)
        result: set[str] = set()
        stack = list(dependents.get(metric, set()))
        while stack:
            current = stack.pop()
            if current in result:
                continue
            result.add(current)
            stack.extend(dependents.get(current, set()))
        return tuple(sorted(result))

    # -- full lineage ------------------------------------------------------ #

    def lineage_of(self, metric: str) -> MetricLineage:
        refs = self.graph.base_measures(metric)
        columns = tuple(
            PhysicalColumn(
                model=ref.model,
                source=self.graph.model(ref.model).source,
                expression=ref.measure.expression,
            )
            for ref in refs
        )
        return MetricLineage(
            metric=metric,
            upstream_metrics=self.upstream(metric),
            base_measures=tuple(ref.key for ref in refs),
            physical_columns=columns,
            models=tuple(sorted({ref.model for ref in refs})),
        )

    # -- edge list --------------------------------------------------------- #

    def to_edges(self, metrics: Iterable[str] | None = None) -> tuple[LineageEdge, ...]:
        """Flat edge list across the requested metrics (or every metric)."""
        names = list(metrics) if metrics is not None else list(self.graph.metrics)
        edges: list[LineageEdge] = []
        seen: set[tuple[str, str]] = set()
        # Expand to include transitive deps so the graph is self-contained.
        full: set[str] = set()
        for name in names:
            for up in self.upstream(name):
                full.add(up)
            full.add(name)
        for name in sorted(full):
            metric = self.graph.metric(name)
            for dep in metric_dependencies(metric):
                self._add_edge(edges, seen, dep, name, NodeKind.METRIC, NodeKind.METRIC)
            if isinstance(metric, SimpleMetric):
                ref = self.graph.resolve_measure(metric.measure, metric.model)
                self._add_edge(
                    edges, seen, ref.key, name, NodeKind.MEASURE, NodeKind.METRIC
                )
                model = self.graph.model(ref.model)
                column = f"{model.source}.{ref.measure.expression}"
                self._add_edge(
                    edges, seen, column, ref.key, NodeKind.COLUMN, NodeKind.MEASURE
                )
                self._add_edge(
                    edges, seen, ref.model, column, NodeKind.MODEL, NodeKind.COLUMN
                )
        return tuple(edges)

    @staticmethod
    def _add_edge(
        edges: list[LineageEdge],
        seen: set[tuple[str, str]],
        source: str,
        target: str,
        source_kind: str,
        target_kind: str,
    ) -> None:
        key = (source, target)
        if key in seen:
            return
        seen.add(key)
        edges.append(
            LineageEdge(
                source=source,
                target=target,
                source_kind=source_kind,
                target_kind=target_kind,
            )
        )


__all__ = [
    "LineageEdge",
    "LineageGraph",
    "MetricLineage",
    "NodeKind",
    "PhysicalColumn",
]
