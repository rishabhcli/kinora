"""The materialization advisor — turn query history into pre-aggregation advice.

A semantic layer earns its keep when hot queries stop re-scanning the warehouse.
The advisor observes the *plans* the compiler produced (it records each compiled
:class:`QueryPlan`, never raw SQL) and recommends **materialized
pre-aggregations**: a (base-model, grouping-grain, dimension-set, measure-set)
rollup that, if built, would satisfy a cluster of observed queries from a much
smaller table.

The logic is deliberately simple and explainable (no ML): a candidate is the
*aggregation shape* of a query — its base model, the set of additive base
measures, the group-by dimensions, and the time grain. Two queries share a shape
if they group by the same dimensions at the same grain over the same model; the
advisor counts shape frequency, scores each by ``frequency × measures`` (a proxy
for scan savings), and a candidate is **recommended** once its frequency crosses
a threshold. Crucially it only recommends a rollup over **additive** measures —
a coarser rollup can be re-summed to answer a finer-or-equal-grain query, which
is exactly the property the compiler's cumulative metrics already rely on.

It also reports **coverage**: given a set of accepted materializations, which of
the observed queries each one could serve (same model, subset of dimensions,
coarser-or-equal grain, subset of measures). Pure + deterministic.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from app.lakehouse.semantic.plan import QueryPlan
from app.lakehouse.semantic.types import (
    Aggregation,
    TimeGrain,
    grain_rank,
)


@dataclass(frozen=True, slots=True)
class AggregationShape:
    """The materialization-relevant shape of one query's aggregation."""

    base_model: str
    dimensions: tuple[str, ...]  # sorted dimension output names (no time)
    grain: TimeGrain | None
    measures: tuple[str, ...]  # sorted aggregate output names
    additive: bool  # all measures additive (eligible for coarser rollup reuse)

    @classmethod
    def of(cls, plan: QueryPlan) -> AggregationShape:
        agg = plan.aggregation
        dims = tuple(sorted(g.output for g in agg.group_keys if not g.is_time))
        measures = tuple(sorted(a.output for a in agg.aggregates))
        additive = all(_is_additive(a.agg) for a in agg.aggregates)
        return cls(
            base_model=agg.base_model,
            dimensions=dims,
            grain=plan.time_grain,
            measures=measures,
            additive=additive,
        )

    def covers(self, query: AggregationShape) -> bool:
        """True if a materialization of *this* shape can serve ``query``.

        Same base model; this rollup's dimensions are a superset (so a query can
        roll *up* to fewer dims); this rollup's measures are a superset; and this
        rollup's grain is finer-or-equal (so the query can roll up in time). Only
        valid for additive measures.
        """
        if not self.additive:
            return False
        if self.base_model != query.base_model:
            return False
        if not set(query.dimensions).issubset(self.dimensions):
            return False
        if not set(query.measures).issubset(self.measures):
            return False
        return _grain_finer_or_equal(self.grain, query.grain)


@dataclass(frozen=True, slots=True)
class Recommendation:
    """A recommended pre-aggregation + why."""

    shape: AggregationShape
    frequency: int
    score: float
    rationale: str


@dataclass
class MaterializationAdvisor:
    """Records observed query plans and recommends materializations."""

    min_frequency: int = 3
    _shapes: Counter[AggregationShape] = field(default_factory=Counter)
    _total: int = 0

    def observe(self, plan: QueryPlan) -> None:
        """Record one compiled query (call from the service after a compile)."""
        # Only additive, dimensional aggregations are materialization candidates.
        shape = AggregationShape.of(plan)
        self._shapes[shape] += 1
        self._total += 1

    def observe_shape(self, shape: AggregationShape, count: int = 1) -> None:
        """Record a shape directly ``count`` times (testing / backfill)."""
        self._shapes[shape] += count
        self._total += count

    @property
    def observations(self) -> int:
        return self._total

    def recommendations(self) -> tuple[Recommendation, ...]:
        """Return scored recommendations above the frequency threshold.

        Scored by ``frequency × (len(measures) + len(dimensions) + 1)`` — a crude
        proxy for the scan a rollup avoids (more columns folded == bigger win).
        Non-additive shapes are reported only if they are themselves hot, but with
        a note that they cannot be reused at a coarser grain.
        """
        recs: list[Recommendation] = []
        for shape, freq in self._shapes.items():
            if freq < self.min_frequency:
                continue
            width = len(shape.measures) + len(shape.dimensions) + 1
            score = float(freq * width)
            if shape.additive:
                rationale = (
                    f"{freq} queries share this rollup over {shape.base_model!r}; "
                    f"materialising {len(shape.measures)} additive measure(s) at "
                    f"grain {shape.grain or 'none'} lets coarser/equal-grain queries "
                    "re-aggregate from a small table."
                )
            else:
                rationale = (
                    f"{freq} queries share this shape but include non-additive "
                    "aggregates; the rollup can only serve exact-shape repeats."
                )
            recs.append(
                Recommendation(
                    shape=shape, frequency=freq, score=score, rationale=rationale
                )
            )
        recs.sort(key=lambda r: (-r.score, r.shape.base_model, r.shape.dimensions))
        return tuple(recs)

    def coverage(
        self, materializations: tuple[AggregationShape, ...]
    ) -> dict[AggregationShape, int]:
        """For each candidate materialization, how many observed queries it serves."""
        out: dict[AggregationShape, int] = {}
        for mat in materializations:
            served = sum(
                count for shape, count in self._shapes.items() if mat.covers(shape)
            )
            out[mat] = served
        return out

    def best_single_materialization(self) -> Recommendation | None:
        """The single rollup that would serve the most observed query volume."""
        candidates = [s for s in self._shapes if s.additive]
        if not candidates:
            return None
        coverage = self.coverage(tuple(candidates))
        best_shape = max(coverage, key=lambda s: (coverage[s], len(s.measures)))
        served = coverage[best_shape]
        if served == 0:
            return None
        width = len(best_shape.measures) + len(best_shape.dimensions) + 1
        return Recommendation(
            shape=best_shape,
            frequency=self._shapes[best_shape],
            score=float(served * width),
            rationale=(
                f"a single rollup over {best_shape.base_model!r} at grain "
                f"{best_shape.grain or 'none'} would serve {served} of "
                f"{self._total} observed queries by re-aggregation."
            ),
        )


def _is_additive(agg: Aggregation) -> bool:
    return agg in (Aggregation.SUM, Aggregation.COUNT, Aggregation.SUM_BOOLEAN)


def _grain_finer_or_equal(rollup: TimeGrain | None, query: TimeGrain | None) -> bool:
    """A rollup at ``rollup`` grain can serve a ``query`` grain if it is finer/equal."""
    if query is None:
        # A non-time query needs a non-time (or any) rollup; only a non-time
        # rollup is safe (a time rollup would still need a final time-collapse,
        # which it can do, so accept either as long as dims/measures match).
        return True
    if rollup is None:
        # A non-time rollup cannot reconstruct a time series.
        return False
    return grain_rank(rollup) <= grain_rank(query)


__all__ = [
    "AggregationShape",
    "MaterializationAdvisor",
    "Recommendation",
]
