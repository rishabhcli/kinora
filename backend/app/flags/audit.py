"""Audit trail — structural diffing of flag/experiment changes (pure).

Every write to a flag or experiment should leave a record of *what changed* and
*who changed it*. The persistence of those records is the store's job
(:class:`~app.flags.db_models.FlagAudit`); this module is the pure part: turning
two serialized definitions into a compact, human-readable diff so the audit log
stores "rollout 10% → 25%, rule 'eu' added" rather than two opaque blobs.

The diff is a flat list of :class:`FieldChange`\\ s over the JSON projection of a
flag/experiment, with nested dict/list paths flattened to dotted keys
(``rules[0].variation``). Pure and dependency-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class AuditAction(StrEnum):
    """The kind of mutation an audit record captures."""

    CREATE = "create"
    UPDATE = "update"
    ARCHIVE = "archive"
    DELETE = "delete"
    TOGGLE = "toggle"  # enabled/disabled flip (a special, common UPDATE)


class ChangeOp(StrEnum):
    """Per-field change operation."""

    ADD = "add"
    REMOVE = "remove"
    REPLACE = "replace"


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One leaf change between two serialized definitions."""

    path: str
    op: ChangeOp
    before: Any
    after: Any

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "op": self.op.value, "before": self.before, "after": self.after}


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a nested JSON value to dotted/indexed leaf paths."""
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = value
    return out


def diff(before: dict[str, Any] | None, after: dict[str, Any] | None) -> list[FieldChange]:
    """Compute the leaf-level changes from ``before`` to ``after``.

    ``before is None`` → all of ``after`` is an ADD (a create); ``after is None``
    → all of ``before`` is a REMOVE (a delete). The result is sorted by path for
    a stable, reviewable audit entry.
    """
    flat_before = _flatten(before) if before is not None else {}
    flat_after = _flatten(after) if after is not None else {}
    changes: list[FieldChange] = []
    for path in set(flat_before) | set(flat_after):
        in_b, in_a = path in flat_before, path in flat_after
        b_val, a_val = flat_before.get(path), flat_after.get(path)
        if in_b and not in_a:
            changes.append(FieldChange(path, ChangeOp.REMOVE, b_val, None))
        elif in_a and not in_b:
            changes.append(FieldChange(path, ChangeOp.ADD, None, a_val))
        elif b_val != a_val:
            changes.append(FieldChange(path, ChangeOp.REPLACE, b_val, a_val))
    return sorted(changes, key=lambda c: c.path)


#: Bookkeeping fields excluded when classifying the *kind* of a change — the
#: store bumps ``version`` on every write, so a pure kill-switch flip would
#: otherwise never read as a TOGGLE.
_BOOKKEEPING_PATHS = frozenset({"version"})


def infer_action(
    before: dict[str, Any] | None, after: dict[str, Any] | None
) -> AuditAction:
    """Classify a change as create/delete/archive/toggle/update.

    ``version`` (and other bookkeeping fields) are ignored for classification so
    a save that flips only ``enabled`` reads as a TOGGLE even though the store
    also incremented the version.
    """
    if before is None:
        return AuditAction.CREATE
    if after is None:
        return AuditAction.DELETE
    if not before.get("archived") and after.get("archived"):
        return AuditAction.ARCHIVE
    changed = {c.path for c in diff(before, after)} - _BOOKKEEPING_PATHS
    if changed == {"enabled"}:
        return AuditAction.TOGGLE
    return AuditAction.UPDATE


def summarize(changes: list[FieldChange], *, limit: int = 8) -> str:
    """A short human-readable summary line for an audit feed."""
    if not changes:
        return "no changes"
    parts = [f"{c.path}: {c.before!r}→{c.after!r}" for c in changes[:limit]]
    if len(changes) > limit:
        parts.append(f"(+{len(changes) - limit} more)")
    return "; ".join(parts)


__all__ = [
    "AuditAction",
    "ChangeOp",
    "FieldChange",
    "diff",
    "infer_action",
    "summarize",
]
