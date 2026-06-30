"""The audit-log query / search vocabulary.

A :class:`AuditQuery` is a declarative filter every sink understands: filter by
actor, target, category/action, severity, correlation/trace id, and time window,
with deterministic ordering and pagination. Keeping the filter as data (not a
method-per-combination) lets the in-memory sink and a DB sink share one contract
and lets the service compose higher-level queries (provenance trail, accountability
slice) on top.

Pure module (dataclasses + a pure ``matches`` predicate the in-memory sink uses).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from app.audit.taxonomy import (
    AuditAction,
    AuditActorKind,
    AuditCategory,
    AuditSeverity,
)

if TYPE_CHECKING:
    from app.audit.store import AuditRecord


@dataclass(frozen=True)
class AuditQuery:
    """A declarative filter over the audit log.

    All set filters are *OR within a field, AND across fields*: e.g.
    ``categories={CANON, RENDER}`` matches either category, but combined with
    ``actor_id="usr_1"`` matches only that actor's canon-or-render events. ``None``
    / empty means "no constraint on this field". Time bounds are inclusive lower,
    exclusive upper (half-open), matched against the entry's logical
    ``occurred_at``.
    """

    actor_kinds: frozenset[AuditActorKind] = field(default_factory=frozenset)
    actor_ids: frozenset[str] = field(default_factory=frozenset)
    categories: frozenset[AuditCategory] = field(default_factory=frozenset)
    actions: frozenset[AuditAction] = field(default_factory=frozenset)
    severities: frozenset[AuditSeverity] = field(default_factory=frozenset)
    target_type: str | None = None
    target_ids: frozenset[str] = field(default_factory=frozenset)
    correlation_id: str | None = None
    trace_id: str | None = None
    since: datetime | None = None  # inclusive lower bound on occurred_at
    until: datetime | None = None  # exclusive upper bound on occurred_at
    #: Result ordering and slice. ``ascending`` over ``seq`` is the default so the
    #: hash-chain order is preserved; flip for "most recent first" views.
    ascending: bool = True
    limit: int | None = None
    offset: int = 0

    def matches(self, record: AuditRecord) -> bool:
        """True iff ``record`` satisfies every constraint in this query."""
        if self.actor_kinds and record.actor_kind not in self.actor_kinds:
            return False
        if self.actor_ids and record.actor_id not in self.actor_ids:
            return False
        if self.categories and record.category not in self.categories:
            return False
        if self.actions and record.action not in self.actions:
            return False
        if self.severities and record.severity not in self.severities:
            return False
        if self.target_type is not None and record.target_type != self.target_type:
            return False
        if self.target_ids and record.target_id not in self.target_ids:
            return False
        if self.correlation_id is not None and record.correlation_id != self.correlation_id:
            return False
        if self.trace_id is not None and record.trace_id != self.trace_id:
            return False
        if self.since is not None and record.occurred_at < self.since:
            return False
        return not (self.until is not None and record.occurred_at >= self.until)


def paginate(records: Sequence[AuditRecord], query: AuditQuery) -> list[AuditRecord]:
    """Apply ordering + offset/limit to an already-filtered record list."""
    ordered = list(records)
    ordered.sort(key=lambda r: r.seq, reverse=not query.ascending)
    sliced = ordered[query.offset :]
    if query.limit is not None:
        sliced = sliced[: query.limit]
    return sliced


__all__ = ["AuditQuery", "paginate"]
