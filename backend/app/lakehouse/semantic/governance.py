"""Governance — row-level + column-level access control over the semantic layer.

Self-serve analytics is only safe if *who is asking* shapes *what they can see*.
This module is the policy engine that sits between the query API and the
compiler. It is **pure policy data + pure enforcement** — no auth, no I/O; the
caller supplies a :class:`Principal` (already authenticated upstream) and the
governance layer decides.

Three enforcement points, all expressed against the *semantic* names (metrics,
dimensions) so policies survive a physical-schema change:

* **Metric allow/deny** — a principal may be restricted to a set of metrics (a
  deny on a requested metric is a hard :class:`AccessDenied`).
* **Column-level** — *sensitive* dimensions can be denied (raise) or **masked**
  (the value replaced by a redaction token in the result). Masking is applied
  post-execution; denial blocks the query at validation.
* **Row-level** — a per-principal row filter (a :class:`FilterExpr`, typically
  ``tenant_id = <principal.tenant>``) is *conjoined* onto the user's filters so a
  principal physically cannot read rows outside their scope. This is the one that
  matters most: it is enforced inside the aggregation, not after.

Policies are resolved through a :class:`PolicyResolver` (a Protocol) so the host
app can back them with roles/tenancy however it likes; :class:`StaticPolicyStore`
is the in-memory default used in tests and for a single-tenant deployment.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.lakehouse.semantic.types import (
    FieldRef,
    FilterExpr,
    and_all,
)

#: The token a masked column value is replaced with.
REDACTED = "***"


class AccessDenied(PermissionError):  # noqa: N818 - a deliberate, public API name
    """Raised when a principal is not permitted to run (part of) a query."""


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated identity a query runs as (supplied by the host app)."""

    subject: str
    roles: frozenset[str] = frozenset()
    tenant: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def has_role(self, role: str) -> bool:
        return role in self.roles


class ColumnAction:
    """What governance does to a sensitive column for a principal."""

    ALLOW = "allow"
    MASK = "mask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class AccessPolicy:
    """The resolved access decision for one principal against one graph.

    ``allowed_metrics`` of ``None`` means *all* metrics are permitted; an explicit
    (possibly empty) set restricts to it. ``column_actions`` maps a dimension
    name to a :class:`ColumnAction`. ``row_filter`` is conjoined onto the query.
    """

    allowed_metrics: frozenset[str] | None = None
    column_actions: Mapping[str, str] = field(default_factory=dict)
    row_filter: FilterExpr | None = None

    def metric_permitted(self, metric: str) -> bool:
        return self.allowed_metrics is None or metric in self.allowed_metrics

    def column_action(self, dimension: str) -> str:
        return self.column_actions.get(dimension, ColumnAction.ALLOW)


class PolicyResolver(Protocol):
    """Resolves the effective :class:`AccessPolicy` for a principal."""

    def resolve(self, principal: Principal) -> AccessPolicy:
        ...


@dataclass
class StaticPolicyStore:
    """A simple role -> policy store, merged for a principal's roles.

    Multiple matching role policies are merged: metric allow-lists intersect
    (most restrictive wins), column actions take the *strongest* (DENY > MASK >
    ALLOW), and row filters are conjoined. An unmatched principal gets the
    ``default`` policy (open unless configured otherwise).
    """

    by_role: dict[str, AccessPolicy] = field(default_factory=dict)
    default: AccessPolicy = field(default_factory=AccessPolicy)

    def resolve(self, principal: Principal) -> AccessPolicy:
        matched = [self.by_role[r] for r in sorted(principal.roles) if r in self.by_role]
        if not matched:
            return self.default
        merged = matched[0]
        for policy in matched[1:]:
            merged = _merge(merged, policy)
        return merged


_ACTION_RANK = {ColumnAction.ALLOW: 0, ColumnAction.MASK: 1, ColumnAction.DENY: 2}


def _merge(a: AccessPolicy, b: AccessPolicy) -> AccessPolicy:
    if a.allowed_metrics is None:
        metrics = b.allowed_metrics
    elif b.allowed_metrics is None:
        metrics = a.allowed_metrics
    else:
        metrics = a.allowed_metrics & b.allowed_metrics
    columns: dict[str, str] = dict(a.column_actions)
    for dim, action in b.column_actions.items():
        if _ACTION_RANK[action] > _ACTION_RANK.get(columns.get(dim, ColumnAction.ALLOW), 0):
            columns[dim] = action
    return AccessPolicy(
        allowed_metrics=metrics,
        column_actions=columns,
        row_filter=and_all(a.row_filter, b.row_filter),
    )


# --------------------------------------------------------------------------- #
# Enforcement
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GovernedQuery:
    """The result of pre-execution governance: the extra row filter + mask plan."""

    row_filter: FilterExpr | None
    masked_dimensions: frozenset[str]


class GovernanceEngine:
    """Applies an :class:`AccessPolicy` to a query (pre) and its result (post)."""

    def __init__(self, resolver: PolicyResolver):
        self._resolver = resolver

    def authorize(
        self,
        principal: Principal,
        *,
        requested_metrics: tuple[str, ...],
        requested_dimensions: tuple[FieldRef, ...],
    ) -> GovernedQuery:
        """Validate access and return the row filter + masked-column plan.

        Raises :class:`AccessDenied` for a denied metric or a denied (sensitive)
        dimension. Returns the conjoinable row filter and the set of dimensions
        to redact in the result.
        """
        policy = self._resolver.resolve(principal)
        for metric in requested_metrics:
            if not policy.metric_permitted(metric):
                raise AccessDenied(
                    f"principal {principal.subject!r} may not query metric {metric!r}"
                )
        masked: set[str] = set()
        for ref in requested_dimensions:
            action = policy.column_action(ref.name)
            if action == ColumnAction.DENY:
                raise AccessDenied(
                    f"principal {principal.subject!r} may not group by {ref.name!r}"
                )
            if action == ColumnAction.MASK:
                masked.add(ref.name)
        return GovernedQuery(
            row_filter=policy.row_filter,
            masked_dimensions=frozenset(masked),
        )

    @staticmethod
    def apply_masking(
        rows: list[dict[str, Any]], masked_dimensions: frozenset[str]
    ) -> list[dict[str, Any]]:
        """Redact masked dimension values in-place (post-execution)."""
        if not masked_dimensions:
            return rows
        for row in rows:
            for dim in masked_dimensions:
                if dim in row:
                    row[dim] = REDACTED
        return rows


__all__ = [
    "REDACTED",
    "AccessDenied",
    "AccessPolicy",
    "ColumnAction",
    "GovernanceEngine",
    "GovernedQuery",
    "Principal",
    "PolicyResolver",
    "StaticPolicyStore",
]
