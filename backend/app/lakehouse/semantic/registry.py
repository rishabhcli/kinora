"""The semantic graph — the validated registry of models + metrics.

:class:`SemanticGraph` is the immutable, fully-validated result of registering a
set of :class:`~app.lakehouse.semantic.model.SemanticModel` and
:class:`~app.lakehouse.semantic.metrics.Metric` definitions. Construction
performs every static check the compiler relies on so that later stages can
assume a consistent world:

* every measure a metric references exists in exactly one (or the pinned) model;
* every metric dependency resolves and the metric DAG is acyclic;
* join targets exist and the join graph is connected enough to resolve any
  cross-model query a metric needs (shortest-path join resolution);
* time/grain constraints (cumulative & comparison metrics ultimately bottom out
  in a measure whose model exposes a time dimension).

The graph also exposes the *base-measure expansion* of any metric (the set of
``(model, measure)`` leaves it depends on) and the topological order of metric
evaluation — the two facts the compiler turns into a query plan.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.lakehouse.semantic.metrics import (
    CumulativeMetric,
    Metric,
    SimpleMetric,
    TimeComparisonMetric,
    metric_dependencies,
)
from app.lakehouse.semantic.model import Measure, SemanticModel
from app.lakehouse.semantic.types import is_additive


@dataclass(frozen=True, slots=True)
class MeasureRef:
    """A fully-resolved base measure: which model owns it + the measure itself."""

    model: str
    measure: Measure

    @property
    def key(self) -> str:
        return f"{self.model}.{self.measure.name}"


class SemanticGraphError(ValueError):
    """Raised when a model/metric set fails static validation."""


@dataclass(frozen=True)
class SemanticGraph:
    """An immutable, validated registry of models + metrics.

    Built via :meth:`build`; the constructor takes already-indexed maps so the
    dataclass stays a cheap value. Most callers use :meth:`build`.
    """

    models: Mapping[str, SemanticModel]
    metrics: Mapping[str, Metric]
    _measure_owner: Mapping[str, str] = field(default_factory=dict, repr=False)
    _topo_order: tuple[str, ...] = field(default=(), repr=False)

    # -- construction ------------------------------------------------------ #

    @classmethod
    def build(
        cls,
        models: Iterable[SemanticModel],
        metrics: Iterable[Metric] = (),
    ) -> SemanticGraph:
        model_map: dict[str, SemanticModel] = {}
        for model in models:
            if model.name in model_map:
                raise SemanticGraphError(f"duplicate model {model.name!r}")
            model_map[model.name] = model

        # Validate join targets exist.
        for model in model_map.values():
            for join in model.joins:
                if join.to_model not in model_map:
                    raise SemanticGraphError(
                        f"model {model.name!r} joins unknown model {join.to_model!r}"
                    )

        # Build a measure-name -> owning-model index (ambiguity is a hard error
        # unless the metric pins a model explicitly).
        measure_owner: dict[str, str] = {}
        ambiguous: set[str] = set()
        for model in model_map.values():
            for measure in model.measures:
                if measure.name in measure_owner:
                    ambiguous.add(measure.name)
                measure_owner[measure.name] = model.name

        metric_map: dict[str, Metric] = {}
        for metric in metrics:
            if metric.name in metric_map:
                raise SemanticGraphError(f"duplicate metric {metric.name!r}")
            if metric.name in measure_owner:
                raise SemanticGraphError(
                    f"metric {metric.name!r} collides with a measure of the same name"
                )
            metric_map[metric.name] = metric

        graph = cls(
            models=model_map,
            metrics=metric_map,
            _measure_owner=measure_owner,
            _topo_order=(),
        )
        graph._validate(ambiguous)
        topo = graph._topological_order()
        object.__setattr__(graph, "_topo_order", topo)
        graph._validate_grain_requirements()
        return graph

    # -- model / measure resolution --------------------------------------- #

    def model(self, name: str) -> SemanticModel:
        try:
            return self.models[name]
        except KeyError:
            raise SemanticGraphError(f"unknown model {name!r}") from None

    def metric(self, name: str) -> Metric:
        try:
            return self.metrics[name]
        except KeyError:
            raise SemanticGraphError(f"unknown metric {name!r}") from None

    def has_metric(self, name: str) -> bool:
        return name in self.metrics

    def resolve_measure(self, name: str, model: str | None = None) -> MeasureRef:
        """Resolve a measure name (optionally model-pinned) to a :class:`MeasureRef`."""
        if model is not None:
            owner = self.model(model)
            if not owner.has_measure(name):
                raise SemanticGraphError(f"model {model!r} has no measure {name!r}")
            return MeasureRef(model=model, measure=owner.measure(name))
        owner_name = self._measure_owner.get(name)
        if owner_name is None:
            raise SemanticGraphError(f"no model defines measure {name!r}")
        return MeasureRef(model=owner_name, measure=self.model(owner_name).measure(name))

    # -- metric expansion -------------------------------------------------- #

    def base_measures(self, metric_name: str) -> tuple[MeasureRef, ...]:
        """Return the deduped set of base measures a metric ultimately needs."""
        seen: dict[str, MeasureRef] = {}
        self._collect_base_measures(metric_name, seen, set())
        return tuple(seen.values())

    def _collect_base_measures(
        self, metric_name: str, out: dict[str, MeasureRef], stack: set[str]
    ) -> None:
        if metric_name in stack:
            raise SemanticGraphError(
                f"metric cycle through {metric_name!r}: {' -> '.join(stack)}"
            )
        metric = self.metric(metric_name)
        if isinstance(metric, SimpleMetric):
            ref = self.resolve_measure(metric.measure, metric.model)
            out[ref.key] = ref
            return
        stack = stack | {metric_name}
        for dep in metric_dependencies(metric):
            self._collect_base_measures(dep, out, stack)

    def metric_models(self, metric_name: str) -> frozenset[str]:
        """The set of models a metric touches (via its base measures)."""
        return frozenset(ref.model for ref in self.base_measures(metric_name))

    def topo_order(self) -> tuple[str, ...]:
        """Metric names in dependency order (deps before dependents)."""
        return self._topo_order

    def dependency_order_for(self, metric_names: Iterable[str]) -> tuple[str, ...]:
        """Topo order restricted to a query's metrics + all their transitive deps."""
        wanted: set[str] = set()
        for name in metric_names:
            self._reachable(name, wanted)
        return tuple(n for n in self._topo_order if n in wanted)

    def _reachable(self, name: str, out: set[str]) -> None:
        if name in out:
            return
        out.add(name)
        for dep in metric_dependencies(self.metric(name)):
            self._reachable(dep, out)

    # -- join resolution --------------------------------------------------- #

    def join_path(self, from_model: str, to_model: str) -> tuple[str, ...]:
        """Shortest model-name path ``from_model -> ... -> to_model`` (BFS).

        Returns ``(from_model,)`` for the trivial path. Raises if the models are
        not connected by declared joins. The compiler uses this to assemble the
        ``FROM`` chain for a cross-model query.
        """
        if from_model == to_model:
            return (from_model,)
        self.model(from_model)
        self.model(to_model)
        prev: dict[str, str] = {from_model: from_model}
        queue: deque[str] = deque([from_model])
        while queue:
            current = queue.popleft()
            for join in self.model(current).joins:
                nxt = join.to_model
                if nxt not in prev:
                    prev[nxt] = current
                    if nxt == to_model:
                        return self._reconstruct_path(prev, from_model, to_model)
                    queue.append(nxt)
        raise SemanticGraphError(f"no join path from {from_model!r} to {to_model!r}")

    @staticmethod
    def _reconstruct_path(
        prev: Mapping[str, str], start: str, end: str
    ) -> tuple[str, ...]:
        path: list[str] = [end]
        cursor = end
        while cursor != start:
            cursor = prev[cursor]
            path.append(cursor)
        path.reverse()
        return tuple(path)

    # -- validation -------------------------------------------------------- #

    def _validate(self, ambiguous: set[str]) -> None:
        for name, metric in self.metrics.items():
            if isinstance(metric, SimpleMetric):
                if metric.model is None and metric.measure in ambiguous:
                    raise SemanticGraphError(
                        f"metric {name!r} measure {metric.measure!r} is defined in "
                        "multiple models; pin one with model="
                    )
                # Resolution itself raises on a missing measure.
                self.resolve_measure(metric.measure, metric.model)
            else:
                for dep in metric_dependencies(metric):
                    if dep not in self.metrics:
                        raise SemanticGraphError(
                            f"metric {name!r} references unknown metric {dep!r}"
                        )

    def _topological_order(self) -> tuple[str, ...]:
        """Kahn's algorithm over the metric DAG; raises on a cycle."""
        indeg: dict[str, int] = dict.fromkeys(self.metrics, 0)
        dependents: dict[str, list[str]] = {n: [] for n in self.metrics}
        for name, metric in self.metrics.items():
            for dep in metric_dependencies(metric):
                indeg[name] += 1
                dependents[dep].append(name)
        ready = deque(sorted(n for n, d in indeg.items() if d == 0))
        order: list[str] = []
        while ready:
            node = ready.popleft()
            order.append(node)
            for dependent in dependents[node]:
                indeg[dependent] -= 1
                if indeg[dependent] == 0:
                    ready.append(dependent)
        if len(order) != len(self.metrics):
            remaining = sorted(set(self.metrics) - set(order))
            raise SemanticGraphError(f"metric dependency cycle among {remaining}")
        return tuple(order)

    def _validate_grain_requirements(self) -> None:
        """Cumulative/comparison metrics must bottom out in a time-bearing model."""
        for name, metric in self.metrics.items():
            if isinstance(metric, (CumulativeMetric, TimeComparisonMetric)):
                models = self.metric_models(name)
                if not any(self.model(m).time_dimensions() for m in models):
                    raise SemanticGraphError(
                        f"metric {name!r} is time-dependent but none of its models "
                        f"{sorted(models)} expose a time dimension"
                    )
            if isinstance(metric, CumulativeMetric):
                for ref in self.base_measures(name):
                    if not is_additive(ref.measure.agg):
                        raise SemanticGraphError(
                            f"cumulative metric {name!r} accumulates non-additive "
                            f"measure {ref.key!r} ({ref.measure.agg})"
                        )


__all__ = [
    "MeasureRef",
    "SemanticGraph",
    "SemanticGraphError",
]
