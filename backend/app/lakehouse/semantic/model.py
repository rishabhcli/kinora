"""The declarative *semantic model* — entities, dimensions, measures, joins.

This is the LookML / dbt-MetricFlow-shaped layer: each :class:`SemanticModel`
describes one logical table (its physical source, its grain/primary entity, its
dimensions, its measures, and the foreign keys that let it join to other
models). Models are *declarative data* — no behaviour beyond validation — so a
deployment can be authored in Python, loaded from YAML, or synthesised in a test
and handed to the registry unchanged.

Nothing here touches a database. A measure carries the SQL *expression* it
aggregates (an opaque column expression over the model's source), but the
semantic layer never inspects that expression — it only ever wraps it in an
aggregation and a grain. That keeps the model portable across the in-memory
engine (which reads a named column) and the SQL fallback (which inlines the
expression).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.lakehouse.semantic.types import (
    Aggregation,
    DataType,
    FieldRef,
    FilterExpr,
    TimeGrain,
    validate_identifier,
)


@dataclass(frozen=True, slots=True)
class Dimension:
    """A groupable / filterable attribute of a model.

    A *time* dimension (``is_time=True``) additionally carries the model's finest
    time grain so the query layer can validate requested grains and the compiler
    can emit the right truncation. ``expr`` is the source column/expression (it
    defaults to the dimension name, the common case).
    """

    name: str
    data_type: DataType = DataType.STRING
    expr: str | None = None
    label: str | None = None
    description: str = ""
    is_time: bool = False
    base_grain: TimeGrain | None = None
    #: Marks a column as sensitive so column-level governance can mask/deny it.
    sensitive: bool = False

    def __post_init__(self) -> None:
        validate_identifier(self.name, what="dimension name")
        if self.is_time and self.base_grain is None:
            raise ValueError(f"time dimension {self.name!r} must declare a base_grain")
        if self.is_time and self.data_type not in (DataType.TIMESTAMP, DataType.DATE):
            raise ValueError(f"time dimension {self.name!r} must be timestamp/date typed")

    @property
    def expression(self) -> str:
        return self.expr or self.name

    @property
    def display_label(self) -> str:
        return self.label or self.name.replace("_", " ").title()


@dataclass(frozen=True, slots=True)
class Measure:
    """A numeric quantity to aggregate — the atom a metric is built from.

    ``agg`` is the aggregation applied to ``expr`` (the source column/expression,
    default the measure name). ``measure_filter`` is an optional always-applied
    predicate (a *measure-level* filter, e.g. ``status = 'accepted'``) that the
    compiler folds into the aggregation so the measure only ever sums matching
    rows. ``non_additive_dimension`` flags a measure that must not be summed
    across a particular dimension (e.g. a balance across time).
    """

    name: str
    agg: Aggregation
    expr: str | None = None
    label: str | None = None
    description: str = ""
    measure_filter: FilterExpr | None = None
    non_additive_dimension: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.name, what="measure name")
        if self.non_additive_dimension is not None:
            validate_identifier(self.non_additive_dimension, what="non-additive dimension")

    @property
    def expression(self) -> str:
        # COUNT(*) has no column expression by convention.
        if self.agg is Aggregation.COUNT and self.expr is None:
            return "*"
        return self.expr or self.name

    @property
    def display_label(self) -> str:
        return self.label or self.name.replace("_", " ").title()


class JoinType:
    """SQL join kinds (string constants; kept simple, no enum churn)."""

    INNER = "inner"
    LEFT = "left"


@dataclass(frozen=True, slots=True)
class Join:
    """A foreign-key edge from this model to another semantic model.

    The join is expressed as ``<this>.<from_key> = <to_model>.<to_key>``. The
    registry builds an undirected graph from these edges and the compiler walks
    the shortest path to resolve a cross-model query. ``many_to_one`` (the
    default) means rows in *this* model map to at most one row in ``to_model`` —
    the only fan-out-safe direction for additive aggregation, which the compiler
    enforces.
    """

    to_model: str
    from_key: str
    to_key: str
    join_type: str = JoinType.LEFT
    many_to_one: bool = True

    def __post_init__(self) -> None:
        validate_identifier(self.to_model, what="join target model")
        validate_identifier(self.from_key, what="join from_key")
        validate_identifier(self.to_key, what="join to_key")
        if self.join_type not in (JoinType.INNER, JoinType.LEFT):
            raise ValueError(f"unsupported join_type {self.join_type!r}")


@dataclass(frozen=True, slots=True)
class SemanticModel:
    """One logical table: a source, a primary entity, dimensions, measures, joins.

    ``source`` is the physical table/view name the engine reads (validated as an
    identifier so it is safe to interpolate; the SQL fallback never inlines
    anything else as a table). ``primary_entity`` names the model's grain key
    (used for join validation and ``count_distinct`` of the entity). The maps are
    built from the declared sequences at construction so lookups are O(1).
    """

    name: str
    source: str
    primary_entity: str
    dimensions: tuple[Dimension, ...] = ()
    measures: tuple[Measure, ...] = ()
    joins: tuple[Join, ...] = ()
    label: str | None = None
    description: str = ""
    _dim_index: Mapping[str, Dimension] = field(default_factory=dict, repr=False, compare=False)
    _measure_index: Mapping[str, Measure] = field(
        default_factory=dict, repr=False, compare=False
    )
    _join_index: Mapping[str, Join] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        validate_identifier(self.name, what="model name")
        validate_identifier(self.source, what="model source")
        validate_identifier(self.primary_entity, what="primary entity")
        dim_index: dict[str, Dimension] = {}
        for dim in self.dimensions:
            if dim.name in dim_index:
                raise ValueError(f"duplicate dimension {dim.name!r} in model {self.name!r}")
            dim_index[dim.name] = dim
        measure_index: dict[str, Measure] = {}
        for measure in self.measures:
            if measure.name in measure_index:
                raise ValueError(f"duplicate measure {measure.name!r} in model {self.name!r}")
            measure_index[measure.name] = measure
        join_index: dict[str, Join] = {}
        for join in self.joins:
            if join.to_model in join_index:
                raise ValueError(
                    f"duplicate join to {join.to_model!r} from model {self.name!r}"
                )
            if join.to_model == self.name:
                raise ValueError(f"model {self.name!r} cannot join to itself")
            join_index[join.to_model] = join
        # frozen dataclass: assign the derived indices via object.__setattr__.
        object.__setattr__(self, "_dim_index", dim_index)
        object.__setattr__(self, "_measure_index", measure_index)
        object.__setattr__(self, "_join_index", join_index)

    # -- lookups ----------------------------------------------------------- #

    def dimension(self, name: str) -> Dimension:
        try:
            return self._dim_index[name]
        except KeyError:
            raise KeyError(f"model {self.name!r} has no dimension {name!r}") from None

    def measure(self, name: str) -> Measure:
        try:
            return self._measure_index[name]
        except KeyError:
            raise KeyError(f"model {self.name!r} has no measure {name!r}") from None

    def has_dimension(self, name: str) -> bool:
        return name in self._dim_index

    def has_measure(self, name: str) -> bool:
        return name in self._measure_index

    def join_to(self, model_name: str) -> Join | None:
        return self._join_index.get(model_name)

    def time_dimensions(self) -> tuple[Dimension, ...]:
        return tuple(d for d in self.dimensions if d.is_time)

    @property
    def display_label(self) -> str:
        return self.label or self.name.replace("_", " ").title()


def field_ref_in_model(model: SemanticModel, ref: FieldRef) -> bool:
    """True if a (possibly unqualified) field ref names a dimension of ``model``."""
    if ref.entity is not None and ref.entity != model.name:
        return False
    return model.has_dimension(ref.name)


def collect_sources(models: Iterable[SemanticModel]) -> dict[str, str]:
    """Map each model name to its physical source (used by the SQL fallback)."""
    return {m.name: m.source for m in models}


__all__ = [
    "Dimension",
    "Join",
    "JoinType",
    "Measure",
    "SemanticModel",
    "collect_sources",
    "field_ref_in_model",
]
