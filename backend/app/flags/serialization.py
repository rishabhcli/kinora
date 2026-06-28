"""JSON (de)serialization for flags, experiments, and snapshots.

The pure model dataclasses are the source of truth; this module is the only
place that translates them to/from JSON-safe ``dict``\\ s. The admin API uses it
for request/response bodies, the store uses it for the JSONB columns, and the
cache uses it for the wire format — keeping exactly one definition of the schema
so the surfaces never drift.

Deserialization is *validating*: it routes structural errors through the model
constructors (which raise :class:`~app.flags.errors.FlagValidationError`), so a
malformed payload is rejected at the boundary rather than producing a half-built
flag that the evaluator then has to defend against.
"""

from __future__ import annotations

from typing import Any

from app.flags.errors import FlagValidationError
from app.flags.experiment import (
    Experiment,
    ExperimentStatus,
    Metric,
    MetricDirection,
    MetricKind,
    Variant,
)
from app.flags.models import (
    Clause,
    Flag,
    FlagKind,
    Operator,
    Prerequisite,
    Rollout,
    Rule,
    Target,
    Variation,
    WeightedVariation,
)

# --------------------------------------------------------------------------- #
# Flag <-> dict
# --------------------------------------------------------------------------- #


def variation_to_dict(v: Variation) -> dict[str, Any]:
    return {"key": v.key, "value": v.value, "name": v.name}


def clause_to_dict(c: Clause) -> dict[str, Any]:
    return {
        "attribute": c.attribute,
        "op": c.op.value,
        "values": list(c.values),
        "negate": c.negate,
    }


def rollout_to_dict(r: Rollout) -> dict[str, Any]:
    return {
        "weights": [{"variation": w.variation, "weight": w.weight} for w in r.weights],
        "bucket_by": r.bucket_by,
        "salt": r.salt,
        "seed": r.seed,
    }


def rule_to_dict(rule: Rule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "clauses": [clause_to_dict(c) for c in rule.clauses],
        "variation": rule.variation,
        "rollout": rollout_to_dict(rule.rollout) if rule.rollout is not None else None,
        "description": rule.description,
    }


def flag_to_dict(flag: Flag) -> dict[str, Any]:
    """Serialize a :class:`Flag` to a JSON-safe dict."""
    return {
        "key": flag.key,
        "kind": flag.kind.value,
        "variations": [variation_to_dict(v) for v in flag.variations],
        "default_variation": flag.default_variation,
        "fallthrough": rollout_to_dict(flag.fallthrough),
        "enabled": flag.enabled,
        "archived": flag.archived,
        "prerequisites": [
            {"flag_key": p.flag_key, "variation": p.variation} for p in flag.prerequisites
        ],
        "targets": [{"variation": t.variation, "keys": sorted(t.keys)} for t in flag.targets],
        "rules": [rule_to_dict(r) for r in flag.rules],
        "version": flag.version,
        "name": flag.name,
        "description": flag.description,
        "tags": list(flag.tags),
    }


def _require(data: dict[str, Any], key: str) -> Any:
    if key not in data:
        raise FlagValidationError(f"missing required field {key!r}")
    return data[key]


def clause_from_dict(data: dict[str, Any]) -> Clause:
    return Clause(
        attribute=_require(data, "attribute"),
        op=Operator(_require(data, "op")),
        values=tuple(data.get("values", ())),
        negate=bool(data.get("negate", False)),
    )


def rollout_from_dict(data: dict[str, Any]) -> Rollout:
    weights = tuple(
        WeightedVariation(w["variation"], int(w["weight"]))
        for w in _require(data, "weights")
    )
    return Rollout(
        weights=weights,
        bucket_by=data.get("bucket_by"),
        salt=data.get("salt"),
        seed=int(data.get("seed", 0)),
    )


def rule_from_dict(data: dict[str, Any]) -> Rule:
    rollout = data.get("rollout")
    return Rule(
        id=_require(data, "id"),
        clauses=tuple(clause_from_dict(c) for c in data.get("clauses", ())),
        variation=data.get("variation"),
        rollout=rollout_from_dict(rollout) if rollout is not None else None,
        description=data.get("description", ""),
    )


def flag_from_dict(data: dict[str, Any]) -> Flag:
    """Build a validated :class:`Flag` from a dict (raises on malformed input)."""
    try:
        return Flag(
            key=_require(data, "key"),
            kind=FlagKind(_require(data, "kind")),
            variations=tuple(
                Variation(v["key"], v["value"], v.get("name", ""))
                for v in _require(data, "variations")
            ),
            default_variation=_require(data, "default_variation"),
            fallthrough=rollout_from_dict(_require(data, "fallthrough")),
            enabled=bool(data.get("enabled", True)),
            archived=bool(data.get("archived", False)),
            prerequisites=tuple(
                Prerequisite(p["flag_key"], p["variation"]) for p in data.get("prerequisites", ())
            ),
            targets=tuple(
                Target(t["variation"], frozenset(t["keys"])) for t in data.get("targets", ())
            ),
            rules=tuple(rule_from_dict(r) for r in data.get("rules", ())),
            version=int(data.get("version", 1)),
            name=data.get("name", ""),
            description=data.get("description", ""),
            tags=tuple(data.get("tags", ())),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FlagValidationError(f"malformed flag payload: {exc}") from exc


# --------------------------------------------------------------------------- #
# Experiment <-> dict
# --------------------------------------------------------------------------- #


def metric_to_dict(m: Metric) -> dict[str, Any]:
    return {
        "key": m.key,
        "kind": m.kind.value,
        "direction": m.direction.value,
        "is_guardrail": m.is_guardrail,
        "guardrail_margin": m.guardrail_margin,
        "name": m.name,
    }


def variant_to_dict(v: Variant) -> dict[str, Any]:
    return {
        "key": v.key,
        "weight": v.weight,
        "is_control": v.is_control,
        "flag_variation": v.flag_variation,
    }


def experiment_to_dict(exp: Experiment) -> dict[str, Any]:
    return {
        "key": exp.key,
        "variants": [variant_to_dict(v) for v in exp.variants],
        "salt": exp.salt,
        "status": exp.status.value,
        "audience": [rule_to_dict(r) for r in exp.audience],
        "traffic_percent": exp.traffic_percent,
        "bucket_by": exp.bucket_by,
        "metrics": [metric_to_dict(m) for m in exp.metrics],
        "version": exp.version,
        "name": exp.name,
        "description": exp.description,
    }


def metric_from_dict(data: dict[str, Any]) -> Metric:
    return Metric(
        key=_require(data, "key"),
        kind=MetricKind(data.get("kind", MetricKind.PROPORTION.value)),
        direction=MetricDirection(data.get("direction", MetricDirection.INCREASE.value)),
        is_guardrail=bool(data.get("is_guardrail", False)),
        guardrail_margin=float(data.get("guardrail_margin", 0.0)),
        name=data.get("name", ""),
    )


def experiment_from_dict(data: dict[str, Any]) -> Experiment:
    """Build a validated :class:`Experiment` from a dict."""
    try:
        return Experiment(
            key=_require(data, "key"),
            variants=tuple(
                Variant(
                    v["key"],
                    int(v["weight"]),
                    is_control=bool(v.get("is_control", False)),
                    flag_variation=v.get("flag_variation"),
                )
                for v in _require(data, "variants")
            ),
            salt=_require(data, "salt"),
            status=ExperimentStatus(data.get("status", ExperimentStatus.DRAFT.value)),
            audience=tuple(rule_from_dict(r) for r in data.get("audience", ())),
            traffic_percent=float(data.get("traffic_percent", 100.0)),
            bucket_by=data.get("bucket_by"),
            metrics=tuple(metric_from_dict(m) for m in data.get("metrics", ())),
            version=int(data.get("version", 1)),
            name=data.get("name", ""),
            description=data.get("description", ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FlagValidationError(f"malformed experiment payload: {exc}") from exc


__all__ = [
    "clause_from_dict",
    "clause_to_dict",
    "experiment_from_dict",
    "experiment_to_dict",
    "flag_from_dict",
    "flag_to_dict",
    "metric_from_dict",
    "metric_to_dict",
    "rollout_from_dict",
    "rollout_to_dict",
    "rule_from_dict",
    "rule_to_dict",
    "variant_to_dict",
    "variation_to_dict",
]
