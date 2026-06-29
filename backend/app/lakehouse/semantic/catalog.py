"""The metrics catalog — the self-serve discovery surface.

Every metric and dimension the semantic layer knows, presented as
search/browse-able entries with their human label, description, kind, format,
governance tags, and a one-line lineage summary. This is what a "metrics
explorer" UI lists and what an LLM/assistant reads to answer "what can I ask
for?".

The catalog is *derived* from the :class:`SemanticGraph` (definitions are the
single source of truth) plus optional per-metric tag overlays (e.g. the §13 KPI
tags). It exposes:

* :meth:`metrics` / :meth:`dimensions` — the full entry lists;
* :meth:`search` — case-insensitive substring match over name/label/description/
  tags, ranked so name/label hits beat description hits;
* :meth:`by_tag` / :meth:`groups` — faceted browse;
* :meth:`describe` — a single rich entry for a metric (with its lineage summary).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from app.lakehouse.semantic.lineage import LineageGraph
from app.lakehouse.semantic.metrics import (
    MetricKind,
    requires_time,
)
from app.lakehouse.semantic.registry import SemanticGraph


@dataclass(frozen=True, slots=True)
class MetricEntry:
    """A catalog entry for one metric."""

    name: str
    label: str
    description: str
    kind: MetricKind
    format: str | None
    tags: tuple[str, ...]
    time_dependent: bool
    base_measures: tuple[str, ...]
    upstream_metrics: tuple[str, ...]
    models: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DimensionEntry:
    """A catalog entry for one dimension (model-qualified)."""

    name: str
    model: str
    label: str
    description: str
    data_type: str
    is_time: bool
    sensitive: bool


@dataclass
class MetricsCatalog:
    """A searchable catalog derived from a semantic graph + optional tag overlay."""

    graph: SemanticGraph
    tags: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._lineage = LineageGraph(self.graph)

    # -- entries ----------------------------------------------------------- #

    def describe(self, name: str) -> MetricEntry:
        metric = self.graph.metric(name)
        lineage = self._lineage.lineage_of(name)
        return MetricEntry(
            name=name,
            label=metric.display_label,
            description=metric.description,
            kind=metric.kind,
            format=metric.format,
            tags=tuple(self.tags.get(name, ())),
            time_dependent=requires_time(metric),
            base_measures=lineage.base_measures,
            upstream_metrics=lineage.upstream_metrics,
            models=lineage.models,
        )

    def metrics(self) -> tuple[MetricEntry, ...]:
        return tuple(self.describe(n) for n in sorted(self.graph.metrics))

    def dimensions(self) -> tuple[DimensionEntry, ...]:
        out: list[DimensionEntry] = []
        for model in self.graph.models.values():
            for dim in model.dimensions:
                out.append(
                    DimensionEntry(
                        name=dim.name,
                        model=model.name,
                        label=dim.display_label,
                        description=dim.description,
                        data_type=dim.data_type.value,
                        is_time=dim.is_time,
                        sensitive=dim.sensitive,
                    )
                )
        out.sort(key=lambda d: (d.model, d.name))
        return tuple(out)

    # -- search + browse --------------------------------------------------- #

    def search(self, query: str, *, limit: int | None = None) -> tuple[MetricEntry, ...]:
        """Ranked substring search over metric name/label/description/tags."""
        needle = query.strip().lower()
        if not needle:
            return ()
        scored: list[tuple[int, str, MetricEntry]] = []
        for entry in self.metrics():
            score = _match_score(entry, needle)
            if score > 0:
                scored.append((score, entry.name, entry))
        scored.sort(key=lambda t: (-t[0], t[1]))
        results = tuple(entry for _, _, entry in scored)
        return results[:limit] if limit is not None else results

    def by_tag(self, tag: str) -> tuple[MetricEntry, ...]:
        return tuple(e for e in self.metrics() if tag in e.tags)

    def groups(self) -> dict[str, tuple[str, ...]]:
        """Map each tag to the sorted metric names carrying it (the facet index)."""
        index: dict[str, list[str]] = {}
        for name, tags in self.tags.items():
            for tag in tags:
                index.setdefault(tag, []).append(name)
        return {tag: tuple(sorted(names)) for tag, names in sorted(index.items())}

    def kinds(self) -> dict[str, tuple[str, ...]]:
        """Map each metric kind to the metric names of that kind."""
        index: dict[str, list[str]] = {}
        for entry in self.metrics():
            index.setdefault(entry.kind.value, []).append(entry.name)
        return {kind: tuple(names) for kind, names in sorted(index.items())}


def _match_score(entry: MetricEntry, needle: str) -> int:
    """Higher = better. Name == 100, name-substr 60, label 40, tag 30, desc 10."""
    score = 0
    if entry.name == needle:
        score = max(score, 100)
    elif needle in entry.name:
        score = max(score, 60)
    if needle in entry.label.lower():
        score = max(score, 40)
    if any(needle in tag.lower() for tag in entry.tags):
        score = max(score, 30)
    if needle in entry.description.lower():
        score = max(score, 10)
    return score


__all__ = [
    "DimensionEntry",
    "MetricEntry",
    "MetricsCatalog",
]
