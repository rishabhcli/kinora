"""Spec **snapshot + breaking-change diff** — the API contract gate (§5.6).

The renderer parses concrete JSON shapes from :mod:`apps.desktop.src.lib.api`.
If the backend silently removes an endpoint, drops a response field, narrows a
type, or makes a previously-optional request field required, the renderer breaks
at runtime with no compile-time signal. This module turns that class of drift
into a **detectable, classifiable diff** between a committed *golden* spec and
the live, enriched one.

Two surfaces:

* :func:`snapshot_spec` — canonicalise the enriched OpenAPI to a deterministic,
  diff-stable JSON string (sorted keys) you commit as the golden file.
* :func:`diff_specs` — compare old → new and return a list of
  :class:`SpecChange`, each tagged ``breaking`` / ``addition`` / ``info``.

What counts as **breaking** (a consumer that worked against ``old`` could now
fail against ``new``):

* an endpoint (path+method) was removed;
* a documented success **response field was removed** or its type narrowed;
* a request field became **newly required** (or a brand-new required field
  appeared);
* a request field's type was narrowed;
* a previously-served 2xx status disappeared.

Additions (new endpoints, new optional fields, new responses) are reported as
non-breaking so the gate can allow forward-compatible growth while still failing
hard on a contract break. Nothing here touches the running app or the network —
it operates purely on two spec dicts.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

_SUCCESS_PREFIX = "2"
_MUTATING = frozenset({"get", "put", "post", "delete", "patch", "head", "options", "trace"})


class ChangeKind(StrEnum):
    """How severe a single spec delta is for an existing consumer."""

    BREAKING = "breaking"
    ADDITION = "addition"
    INFO = "info"


@dataclass(frozen=True)
class SpecChange:
    """One classified delta between two OpenAPI documents."""

    kind: ChangeKind
    category: str  # e.g. "endpoint_removed", "response_field_removed"
    location: str  # human path, e.g. "POST /api/sessions -> 200 -> body.session_id"
    detail: str = ""

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"[{self.kind.value}] {self.category}: {self.location} — {self.detail}"


@dataclass
class DiffResult:
    """The full set of changes plus convenience views for the gate."""

    changes: list[SpecChange] = field(default_factory=list)

    @property
    def breaking(self) -> list[SpecChange]:
        return [c for c in self.changes if c.kind is ChangeKind.BREAKING]

    @property
    def additions(self) -> list[SpecChange]:
        return [c for c in self.changes if c.kind is ChangeKind.ADDITION]

    @property
    def is_compatible(self) -> bool:
        """True when nothing breaking changed (forward-compatible)."""
        return not self.breaking

    def summary(self) -> str:
        return (
            f"{len(self.breaking)} breaking, {len(self.additions)} additions, "
            f"{len(self.changes)} total changes"
        )


def snapshot_spec(spec: dict[str, Any]) -> str:
    """Canonicalise a spec to a deterministic, diff-stable JSON string.

    Sorted keys + stable separators make a committed golden file produce a clean
    line diff when (and only when) the contract actually changes.
    """
    return json.dumps(spec, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def load_snapshot(text: str) -> dict[str, Any]:
    """Parse a snapshot string back into a spec dict."""
    return json.loads(text)


# --------------------------------------------------------------------------- #
# Schema resolution + type comparison
# --------------------------------------------------------------------------- #


def _resolve(
    spec: dict[str, Any], schema: Any, _seen: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Resolve a (possibly ``$ref``) schema to an inline object, cycle-safe."""
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


def _type_tokens(spec: dict[str, Any], schema: Any) -> set[str]:
    """The set of accepted JSON types for a schema (handles anyOf/oneOf/nullable).

    Used for *narrowing* detection: ``old`` types ⊄ ``new`` types means a value
    that used to validate may now be rejected (a breaking narrowing).
    """
    resolved = _resolve(spec, schema)
    if not resolved:
        return set()
    tokens: set[str] = set()
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in resolved.get(key, []) or []:
            tokens |= _type_tokens(spec, sub)
    t = resolved.get("type")
    if isinstance(t, str):
        tokens.add(t)
    elif isinstance(t, list):
        tokens.update(x for x in t if isinstance(x, str))
    if resolved.get("nullable"):
        tokens.add("null")
    return tokens


def _object_properties(spec: dict[str, Any], schema: Any) -> tuple[dict[str, Any], set[str]]:
    """Return (properties, required-set) for an object schema (allOf-merged)."""
    resolved = _resolve(spec, schema)
    props: dict[str, Any] = dict(resolved.get("properties", {}) or {})
    required: set[str] = set(resolved.get("required", []) or [])
    for sub in resolved.get("allOf", []) or []:
        sub_props, sub_req = _object_properties(spec, sub)
        props.update(sub_props)
        required |= sub_req
    return props, required


def _json_body_schema(spec: dict[str, Any], op: dict[str, Any], code: str) -> Any:
    """Extract the JSON response schema for a status code, or ``None``."""
    resp = (op.get("responses") or {}).get(code)
    if not isinstance(resp, dict):
        return None
    content = resp.get("content") or {}
    media = content.get("application/json") or {}
    return media.get("schema")


def _request_body_schema(spec: dict[str, Any], op: dict[str, Any]) -> Any:
    body = op.get("requestBody")
    if not isinstance(body, dict):
        return None
    content = body.get("content") or {}
    media = content.get("application/json") or {}
    return media.get("schema")


# --------------------------------------------------------------------------- #
# The diff itself
# --------------------------------------------------------------------------- #


def _iter_operations(spec: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() in _MUTATING and isinstance(op, dict):
                yield path, method.lower(), op


def _success_codes(op: dict[str, Any]) -> set[str]:
    return {c for c in (op.get("responses") or {}) if str(c).startswith(_SUCCESS_PREFIX)}


def _diff_response_fields(
    old_spec: dict[str, Any],
    new_spec: dict[str, Any],
    old_op: dict[str, Any],
    new_op: dict[str, Any],
    where: str,
    out: list[SpecChange],
) -> None:
    """Compare success-response object fields old → new (removal/narrowing)."""
    old_codes = _success_codes(old_op)
    new_codes = _success_codes(new_op)
    for code in old_codes - new_codes:
        out.append(
            SpecChange(
                ChangeKind.BREAKING,
                "success_status_removed",
                f"{where} -> {code}",
                "a previously-served success status is gone",
            )
        )
    for code in sorted(old_codes & new_codes):
        old_schema = _json_body_schema(old_spec, old_op, code)
        new_schema = _json_body_schema(new_spec, new_op, code)
        if old_schema is None or new_schema is None:
            continue
        old_props, _ = _object_properties(old_spec, old_schema)
        new_props, _ = _object_properties(new_spec, new_schema)
        for name in old_props:
            loc = f"{where} -> {code} -> response.{name}"
            if name not in new_props:
                out.append(
                    SpecChange(
                        ChangeKind.BREAKING,
                        "response_field_removed",
                        loc,
                        "a field the client may read was removed",
                    )
                )
                continue
            old_t = _type_tokens(old_spec, old_props[name])
            new_t = _type_tokens(new_spec, new_props[name])
            if old_t and new_t and not old_t.issubset(new_t):
                out.append(
                    SpecChange(
                        ChangeKind.BREAKING,
                        "response_type_narrowed",
                        loc,
                        f"type narrowed {sorted(old_t)} -> {sorted(new_t)}",
                    )
                )
        for name in new_props.keys() - old_props.keys():
            out.append(
                SpecChange(
                    ChangeKind.ADDITION,
                    "response_field_added",
                    f"{where} -> {code} -> response.{name}",
                    "a new response field (forward-compatible)",
                )
            )


def _diff_request_fields(
    old_spec: dict[str, Any],
    new_spec: dict[str, Any],
    old_op: dict[str, Any],
    new_op: dict[str, Any],
    where: str,
    out: list[SpecChange],
) -> None:
    """Compare request-body fields old → new (new-required / type narrowing)."""
    old_schema = _request_body_schema(old_spec, old_op)
    new_schema = _request_body_schema(new_spec, new_op)
    if new_schema is None:
        return
    old_props, old_req = _object_properties(old_spec, old_schema) if old_schema else ({}, set())
    new_props, new_req = _object_properties(new_spec, new_schema)
    for name in new_req:
        loc = f"{where} -> request.{name}"
        if name not in old_props:
            out.append(
                SpecChange(
                    ChangeKind.BREAKING,
                    "request_required_field_added",
                    loc,
                    "a brand-new required request field rejects old callers",
                )
            )
        elif name not in old_req:
            out.append(
                SpecChange(
                    ChangeKind.BREAKING,
                    "request_field_now_required",
                    loc,
                    "a previously-optional field is now required",
                )
            )
    for name in old_props.keys() & new_props.keys():
        old_t = _type_tokens(old_spec, old_props[name])
        new_t = _type_tokens(new_spec, new_props[name])
        # Request narrowing is breaking the *other* direction: new must accept
        # everything old accepted, i.e. old ⊆ new for inputs too.
        if old_t and new_t and not old_t.issubset(new_t):
            out.append(
                SpecChange(
                    ChangeKind.BREAKING,
                    "request_type_narrowed",
                    f"{where} -> request.{name}",
                    f"accepted type narrowed {sorted(old_t)} -> {sorted(new_t)}",
                )
            )


def diff_specs(old: dict[str, Any], new: dict[str, Any]) -> DiffResult:
    """Compute the classified diff between two OpenAPI documents (old → new)."""
    out: list[SpecChange] = []
    old_ops = {(p, m): op for p, m, op in _iter_operations(old)}
    new_ops = {(p, m): op for p, m, op in _iter_operations(new)}

    for key in old_ops.keys() - new_ops.keys():
        path, method = key
        out.append(
            SpecChange(
                ChangeKind.BREAKING,
                "endpoint_removed",
                f"{method.upper()} {path}",
                "an endpoint a client may call no longer exists",
            )
        )
    for key in new_ops.keys() - old_ops.keys():
        path, method = key
        out.append(
            SpecChange(
                ChangeKind.ADDITION,
                "endpoint_added",
                f"{method.upper()} {path}",
                "a new endpoint (forward-compatible)",
            )
        )
    for key in sorted(old_ops.keys() & new_ops.keys()):
        path, method = key
        where = f"{method.upper()} {path}"
        old_op, new_op = old_ops[key], new_ops[key]
        _diff_response_fields(old, new, old_op, new_op, where, out)
        _diff_request_fields(old, new, old_op, new_op, where, out)
        # operationId churn is informational — it doesn't break a JSON client but
        # *does* break a generated SDK, so surface it (non-breaking by default).
        old_id, new_id = old_op.get("operationId"), new_op.get("operationId")
        if old_id and new_id and old_id != new_id:
            out.append(
                SpecChange(
                    ChangeKind.INFO,
                    "operation_id_changed",
                    where,
                    f"{old_id} -> {new_id} (regenerate the typed client)",
                )
            )
    return DiffResult(changes=out)


__all__ = [
    "ChangeKind",
    "DiffResult",
    "SpecChange",
    "diff_specs",
    "load_snapshot",
    "snapshot_spec",
]
