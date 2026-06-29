"""Declarative loader — author the semantic model + metrics as plain data.

*Metrics as code* means the definitions live as version-controlled, reviewable
data, not buried in Python. This loader turns a plain ``dict`` (the shape you get
from ``yaml.safe_load`` or ``json.load``) into a validated
:class:`~app.lakehouse.semantic.registry.SemanticGraph`. No YAML dependency is
imposed: the loader takes already-parsed dicts, so the host picks the format.

The schema (illustrative)::

    models:
      - name: shots
        source: fact_shots
        primary_entity: shot_id
        dimensions:
          - {name: agent_role}
          - {name: rendered_at, type: timestamp, is_time: true, grain: hour}
        measures:
          - {name: total_seconds, agg: sum, expr: seconds}
          - name: rejected_seconds
            agg: sum
            expr: seconds
            filter: {field: accepted, op: eq, value: false}
        joins:
          - {to: books, from_key: book_id, to_key: book_id}
    metrics:
      - {name: shots, kind: simple, measure: shot_count}
      - name: efficiency
        kind: derived
        expr: (1 - rejected / total) * 100
        inputs: {rejected: rejected_seconds, total: total_seconds}

Every field is validated; unknown keys raise so typos surface at load time. The
filter mini-language (``{field, op, value}`` / ``{and|or: [...]}`` / ``{not:
...}``) round-trips the :mod:`types` AST.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.lakehouse.semantic.metrics import (
    CalculationKind,
    CumulativeMetric,
    DerivedMetric,
    Metric,
    RatioMetric,
    SimpleMetric,
    TimeComparisonMetric,
    WindowKind,
)
from app.lakehouse.semantic.model import (
    Dimension,
    Join,
    JoinType,
    Measure,
    SemanticModel,
)
from app.lakehouse.semantic.registry import SemanticGraph
from app.lakehouse.semantic.types import (
    Aggregation,
    And,
    Comparison,
    DataType,
    FieldRef,
    FilterExpr,
    Not,
    Or,
    Predicate,
    TimeGrain,
)


class LoaderError(ValueError):
    """Raised when a declarative spec is malformed."""


def _require(d: Mapping[str, Any], key: str, what: str) -> Any:
    if key not in d:
        raise LoaderError(f"{what} is missing required key {key!r}")
    return d[key]


def _check_keys(d: Mapping[str, Any], allowed: set[str], what: str) -> None:
    extra = set(d) - allowed
    if extra:
        raise LoaderError(f"{what} has unknown keys {sorted(extra)}")


# --------------------------------------------------------------------------- #
# Filter mini-language
# --------------------------------------------------------------------------- #


def load_filter(spec: Mapping[str, Any] | None) -> FilterExpr | None:
    """Parse a filter spec dict into a :class:`FilterExpr` (or ``None``)."""
    if spec is None:
        return None
    if "and" in spec:
        return And(tuple(_load_filter_required(t) for t in spec["and"]))
    if "or" in spec:
        return Or(tuple(_load_filter_required(t) for t in spec["or"]))
    if "not" in spec:
        return Not(_load_filter_required(spec["not"]))
    _check_keys(spec, {"field", "op", "value", "entity"}, "filter predicate")
    field = FieldRef(name=_require(spec, "field", "filter"), entity=spec.get("entity"))
    op = Comparison(_require(spec, "op", "filter"))
    if op in (Comparison.IS_NULL, Comparison.IS_NOT_NULL):
        return Predicate(field=field, op=op)
    value = spec.get("value")
    if op in (Comparison.IN, Comparison.NOT_IN):
        if not isinstance(value, (list, tuple)):
            raise LoaderError(f"filter op {op} requires a list value")
        return Predicate(field=field, op=op, value=tuple(value))
    return Predicate(field=field, op=op, value=value)


def _load_filter_required(spec: Mapping[str, Any]) -> FilterExpr:
    result = load_filter(spec)
    if result is None:  # pragma: no cover - only None when spec is None
        raise LoaderError("nested filter cannot be empty")
    return result


# --------------------------------------------------------------------------- #
# Model parts
# --------------------------------------------------------------------------- #


def load_dimension(spec: Mapping[str, Any]) -> Dimension:
    _check_keys(
        spec,
        {"name", "type", "expr", "label", "description", "is_time", "grain", "sensitive"},
        "dimension",
    )
    is_time = bool(spec.get("is_time", False))
    grain = TimeGrain(spec["grain"]) if "grain" in spec else None
    return Dimension(
        name=_require(spec, "name", "dimension"),
        data_type=DataType(spec.get("type", DataType.STRING.value)),
        expr=spec.get("expr"),
        label=spec.get("label"),
        description=spec.get("description", ""),
        is_time=is_time,
        base_grain=grain,
        sensitive=bool(spec.get("sensitive", False)),
    )


def load_measure(spec: Mapping[str, Any]) -> Measure:
    _check_keys(
        spec,
        {"name", "agg", "expr", "label", "description", "filter", "non_additive_dimension"},
        "measure",
    )
    return Measure(
        name=_require(spec, "name", "measure"),
        agg=Aggregation(_require(spec, "agg", "measure")),
        expr=spec.get("expr"),
        label=spec.get("label"),
        description=spec.get("description", ""),
        measure_filter=load_filter(spec.get("filter")),
        non_additive_dimension=spec.get("non_additive_dimension"),
    )


def load_join(spec: Mapping[str, Any]) -> Join:
    _check_keys(spec, {"to", "from_key", "to_key", "type", "many_to_one"}, "join")
    return Join(
        to_model=_require(spec, "to", "join"),
        from_key=_require(spec, "from_key", "join"),
        to_key=_require(spec, "to_key", "join"),
        join_type=spec.get("type", JoinType.LEFT),
        many_to_one=bool(spec.get("many_to_one", True)),
    )


def load_model(spec: Mapping[str, Any]) -> SemanticModel:
    _check_keys(
        spec,
        {
            "name",
            "source",
            "primary_entity",
            "dimensions",
            "measures",
            "joins",
            "label",
            "description",
        },
        "model",
    )
    return SemanticModel(
        name=_require(spec, "name", "model"),
        source=_require(spec, "source", "model"),
        primary_entity=_require(spec, "primary_entity", "model"),
        dimensions=tuple(load_dimension(d) for d in spec.get("dimensions", ())),
        measures=tuple(load_measure(m) for m in spec.get("measures", ())),
        joins=tuple(load_join(j) for j in spec.get("joins", ())),
        label=spec.get("label"),
        description=spec.get("description", ""),
    )


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


_COMMON_METRIC_KEYS = {"name", "kind", "label", "description", "format"}


def load_metric(spec: Mapping[str, Any]) -> Metric:
    kind = _require(spec, "kind", "metric")
    name = _require(spec, "name", "metric")
    common: dict[str, Any] = {
        "name": name,
        "label": spec.get("label"),
        "description": spec.get("description", ""),
        "format": spec.get("format"),
    }
    if kind == "simple":
        _check_keys(spec, _COMMON_METRIC_KEYS | {"measure", "model", "filter"}, "simple metric")
        return SimpleMetric(
            measure=_require(spec, "measure", "simple metric"),
            model=spec.get("model"),
            metric_filter=load_filter(spec.get("filter")),
            **common,
        )
    if kind == "ratio":
        _check_keys(spec, _COMMON_METRIC_KEYS | {"numerator", "denominator"}, "ratio metric")
        return RatioMetric(
            numerator=_require(spec, "numerator", "ratio metric"),
            denominator=_require(spec, "denominator", "ratio metric"),
            **common,
        )
    if kind == "derived":
        _check_keys(spec, _COMMON_METRIC_KEYS | {"expr", "inputs"}, "derived metric")
        return DerivedMetric(
            expr=_require(spec, "expr", "derived metric"),
            inputs=dict(_require(spec, "inputs", "derived metric")),
            **common,
        )
    if kind == "cumulative":
        _check_keys(spec, _COMMON_METRIC_KEYS | {"base", "window", "periods"}, "cumulative metric")
        return CumulativeMetric(
            base=_require(spec, "base", "cumulative metric"),
            window=WindowKind(spec.get("window", WindowKind.ALL_TIME.value)),
            periods=spec.get("periods"),
            **common,
        )
    if kind == "time_comparison":
        _check_keys(
            spec,
            _COMMON_METRIC_KEYS | {"base", "offset_periods", "calculation"},
            "time-comparison metric",
        )
        return TimeComparisonMetric(
            base=_require(spec, "base", "time-comparison metric"),
            offset_periods=int(spec.get("offset_periods", 1)),
            calculation=CalculationKind(
                spec.get("calculation", CalculationKind.PERCENT_CHANGE.value)
            ),
            **common,
        )
    raise LoaderError(f"unknown metric kind {kind!r} for metric {name!r}")


# --------------------------------------------------------------------------- #
# Top-level
# --------------------------------------------------------------------------- #


def load_graph(spec: Mapping[str, Any]) -> SemanticGraph:
    """Build a validated :class:`SemanticGraph` from a declarative spec dict."""
    _check_keys(spec, {"models", "metrics", "version"}, "semantic spec")
    models_spec: Sequence[Mapping[str, Any]] = spec.get("models", ())
    if not models_spec:
        raise LoaderError("a semantic spec must declare at least one model")
    models = [load_model(m) for m in models_spec]
    metrics = [load_metric(m) for m in spec.get("metrics", ())]
    return SemanticGraph.build(models, metrics)


__all__ = [
    "LoaderError",
    "load_dimension",
    "load_filter",
    "load_graph",
    "load_join",
    "load_measure",
    "load_metric",
    "load_model",
]
