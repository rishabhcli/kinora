"""Audit records for runtime-config changes (reuses the §13 pure differ).

Every plane mutation produces a :class:`PlaneAuditRecord`: who changed what flag,
the kind of change, a structural diff of the before/after overlay (computed by
:func:`app.flags.audit.diff`, reused rather than reimplemented), and a one-line
human summary. The records are append-only and held in a bounded ring by the
store so the admin "history" surface is cheap and never unbounded.

Pure: the differ does the structural work; this module only shapes the record.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.flags.audit import diff, summarize
from app.flags.plane.subscriptions import ChangeKind


@dataclass(frozen=True, slots=True)
class PlaneAuditRecord:
    """One append-only audit entry for a runtime-config change."""

    flag_key: str | None
    kind: ChangeKind
    actor: str | None
    summary: str
    changes: tuple[dict[str, Any], ...]
    layer_version: int
    at: float = field(default_factory=lambda: time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "flag_key": self.flag_key,
            "kind": self.kind.value,
            "actor": self.actor,
            "summary": self.summary,
            "changes": list(self.changes),
            "layer_version": self.layer_version,
            "at": self.at,
        }


def build_record(
    *,
    flag_key: str | None,
    kind: ChangeKind,
    actor: str | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    layer_version: int,
) -> PlaneAuditRecord:
    """Diff ``before`` -> ``after`` into a :class:`PlaneAuditRecord`."""
    changes = diff(before, after)
    return PlaneAuditRecord(
        flag_key=flag_key,
        kind=kind,
        actor=actor,
        summary=summarize(changes),
        changes=tuple(c.to_dict() for c in changes),
        layer_version=layer_version,
    )


__all__ = ["PlaneAuditRecord", "build_record"]
