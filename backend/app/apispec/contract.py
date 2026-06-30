"""Contract **harness**: does a live response match its documented schema? (§5.6)

The enricher guarantees the spec is *complete*; this guarantees it is *true*. A
documented response is only useful if the running endpoint actually returns that
shape. :func:`check_response` validates a concrete JSON payload against the
schema the spec documents for a given operation + status, and reports each
mismatch as a :class:`ContractViolation` (missing required field, wrong type,
unexpected null). :func:`check_recorded` drives a list of recorded
``(method, path, status, json)`` samples — e.g. captured from a FastAPI
``TestClient`` — through the same validator.

This is a *structural* validator, not a full JSON-Schema engine: it covers the
shapes Kinora's contracts actually use (objects, arrays, scalars, ``anyOf``
unions, nullability, ``$ref``) which is exactly what catches doc/impl drift —
the spec claims ``budget_remaining_s: number`` but the handler returns a string,
or drops a documented field. It never makes a network call; the caller supplies
the recorded payloads, keeping the harness deterministic and infra-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ContractViolation:
    """One place a live payload diverged from its documented schema."""

    location: str  # human path: "POST /api/sessions -> 200 -> session_id"
    reason: str

    def __str__(self) -> str:  # pragma: no cover - convenience
        return f"{self.location}: {self.reason}"


@dataclass
class ContractReport:
    """The result of validating one or more responses."""

    violations: list[ContractViolation] = field(default_factory=list)
    checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.violations

    def summary(self) -> str:
        return f"{self.checked} responses checked, {len(self.violations)} violations"


_JSON_TYPE_OF = {
    bool: "boolean",
    int: "integer",
    float: "number",
    str: "string",
    type(None): "null",
    list: "array",
    dict: "object",
}


def _json_type(value: Any) -> str:
    # bool is a subclass of int — check it first.
    if isinstance(value, bool):
        return "boolean"
    for py_type, name in _JSON_TYPE_OF.items():
        if py_type is bool:
            continue
        if isinstance(value, py_type):
            return name
    return "unknown"


def _resolve(
    spec: dict[str, Any], schema: Any, _seen: frozenset[str] = frozenset()
) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        if ref in _seen:
            return {}
        target: Any = spec
        for part in ref.lstrip("#/").split("/"):
            if not isinstance(target, dict):
                return {}
            target = target.get(part, {})
        return _resolve(spec, target, _seen | {ref})
    return schema


def _type_accepts(spec: dict[str, Any], schema: dict[str, Any], json_type: str) -> bool:
    """Does ``schema`` permit a value of the given JSON ``json_type``?

    ``integer`` is accepted wherever ``number`` is (JSON has one numeric type),
    and a schema with no ``type`` (e.g. a bare ``{}`` or a pure ``anyOf``) is
    permissive.
    """
    declared = schema.get("type")
    if declared is None:
        return True
    allowed = {declared} if isinstance(declared, str) else set(declared)
    if schema.get("nullable"):
        allowed.add("null")
    # integer is accepted wherever number is (JSON has one numeric type).
    return json_type in allowed or (json_type == "integer" and "number" in allowed)


def _validate(
    spec: dict[str, Any],
    schema: Any,
    value: Any,
    loc: str,
    out: list[ContractViolation],
) -> None:
    resolved = _resolve(spec, schema)
    if not resolved:
        return  # nothing documented to check against

    # Union schemas: the value must satisfy at least one branch.
    for key in ("anyOf", "oneOf"):
        if key in resolved:
            branches = resolved[key]
            for branch in branches:
                trial: list[ContractViolation] = []
                _validate(spec, branch, value, loc, trial)
                if not trial:
                    return
            types = sorted({t for b in branches for t in _branch_types(spec, b)})
            out.append(
                ContractViolation(loc, f"value type {_json_type(value)!r} matches none of {types}")
            )
            return
    if "allOf" in resolved:
        merged = _merge_all_of(spec, resolved["allOf"])
        _validate(spec, merged, value, loc, out)
        return

    json_type = _json_type(value)
    if not _type_accepts(spec, resolved, json_type):
        out.append(
            ContractViolation(
                loc,
                f"expected type {resolved.get('type')!r}, got {json_type!r}",
            )
        )
        return

    if json_type == "object" and isinstance(value, dict):
        props: dict[str, Any] = resolved.get("properties", {}) or {}
        required = set(resolved.get("required", []) or [])
        for name in required:
            if name not in value:
                out.append(ContractViolation(f"{loc}.{name}", "required field missing"))
        for name, sub in props.items():
            if name in value:
                _validate(spec, sub, value[name], f"{loc}.{name}", out)
    elif json_type == "array" and isinstance(value, list):
        items = resolved.get("items")
        if items is not None:
            for i, elem in enumerate(value):
                _validate(spec, items, elem, f"{loc}[{i}]", out)


def _branch_types(spec: dict[str, Any], schema: Any) -> set[str]:
    resolved = _resolve(spec, schema)
    t = resolved.get("type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return set(t)
    return {"any"}


def _merge_all_of(spec: dict[str, Any], parts: list[Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    for part in parts:
        resolved = _resolve(spec, part)
        merged["properties"].update(resolved.get("properties", {}) or {})
        merged["required"] = list(set(merged["required"]) | set(resolved.get("required", []) or []))
        if resolved.get("type") and resolved["type"] != "object":
            merged["type"] = resolved["type"]
    return merged


def _operation(spec: dict[str, Any], method: str, path: str) -> dict[str, Any] | None:
    item = (spec.get("paths") or {}).get(path)
    if not isinstance(item, dict):
        return None
    op = item.get(method.lower())
    return op if isinstance(op, dict) else None


def documented_schema(spec: dict[str, Any], method: str, path: str, status: int) -> Any:
    """The JSON response schema the spec documents for an operation + status."""
    op = _operation(spec, method, path)
    if op is None:
        return None
    resp = (op.get("responses") or {}).get(str(status))
    if not isinstance(resp, dict):
        return None
    media = (resp.get("content") or {}).get("application/json") or {}
    return media.get("schema")


def check_response(
    spec: dict[str, Any],
    method: str,
    path: str,
    status: int,
    payload: Any,
) -> ContractReport:
    """Validate one live ``payload`` against its documented response schema.

    Returns a report whose ``violations`` is empty when the live shape conforms.
    A response with no documented JSON schema (e.g. a ``204`` or a streaming
    route) is treated as conformant (nothing to contradict).
    """
    report = ContractReport()
    schema = documented_schema(spec, method, path, status)
    if schema is None:
        return report
    report.checked = 1
    _validate(
        spec,
        schema,
        payload,
        f"{method.upper()} {path} -> {status} -> body",
        report.violations,
    )
    return report


def check_recorded(
    spec: dict[str, Any],
    samples: list[tuple[str, str, int, Any]],
) -> ContractReport:
    """Validate a batch of recorded ``(method, path, status, json)`` responses.

    ``path`` may be a *concrete* request path (``/api/books/abc123``); it is
    matched back to the templated spec path (``/api/books/{book_id}``) so callers
    can feed raw ``TestClient`` responses without templating by hand.
    """
    report = ContractReport()
    for method, path, status, payload in samples:
        template = _match_template(spec, method, path)
        if template is None:
            continue
        sub = check_response(spec, method, template, status, payload)
        report.checked += sub.checked
        report.violations.extend(sub.violations)
    return report


def _match_template(spec: dict[str, Any], method: str, concrete: str) -> str | None:
    """Resolve a concrete request path back to its templated spec path."""
    paths = spec.get("paths") or {}
    concrete = concrete.split("?", 1)[0]
    if concrete in paths and _operation(spec, method, concrete) is not None:
        return concrete
    target_parts = concrete.strip("/").split("/")
    for template in paths:
        if _operation(spec, method, template) is None:
            continue
        tmpl_parts = template.strip("/").split("/")
        if len(tmpl_parts) != len(target_parts):
            continue
        if all(
            re.fullmatch(r"\{.+?\}", tp) or tp == cp
            for tp, cp in zip(tmpl_parts, target_parts, strict=True)
        ):
            return template
    return None


__all__ = [
    "ContractReport",
    "ContractViolation",
    "check_recorded",
    "check_response",
    "documented_schema",
]
