"""The tenant-scoped query guard — structural cross-tenant isolation.

The single most dangerous failure mode of a multi-tenant data plane is a query
that *forgets* its tenant filter and so returns (or mutates) another tenant's
rows. This module makes that failure **structurally impossible** for any query
routed through it, in two layers:

* :func:`tenant_scoped` — the *applicator*. Given a SQLAlchemy 2.0 ``select`` (or
  ``update``/``delete``) over a tenant-owned entity, it appends the active
  tenant's filter (``entity.<tenant_column> == <tenant_key>``). The caller never
  hand-writes the tenant predicate, so it can't get it wrong or omit it.
* :func:`assert_scoped` / :func:`guard_select` — the *verifier*. It introspects a
  statement's WHERE clause and **raises** :class:`UnscopedQueryError` if no
  predicate constrains the entity's tenant column. This catches a hand-rolled
  query that bypassed :func:`tenant_scoped`, before it ever reaches the DB.

Together they give belt-and-braces isolation: scoped queries are built *with* the
filter, and any query that slips through *without* one is rejected.

The guard works on pure SQLAlchemy Core/ORM constructs and the column-element
tree — **no DB connection, no session, no network** — so it is fully unit-testable
against an in-memory metadata. The tenant *value* comes from the active
:class:`~app.tenancy.context.TenantContext` unless one is passed explicitly.

A model opts into the guard by declaring which column carries the tenant key.
The convention is a column named ``tenant_key`` / ``tenant_id`` / ``org_id`` /
``workspace_id``; a model may override via a ``__tenant_column__`` class
attribute. :func:`tenant_column_name` resolves it.
"""

from __future__ import annotations

from typing import Any, TypeVar

from sqlalchemy import ColumnElement, and_
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression
from sqlalchemy.sql.expression import (
    BooleanClauseList,
    Delete,
    Select,
    Update,
)

from app.tenancy.context import TenantContext, require_tenant

# Statements the guard understands (all carry a ``.whereclause`` / ``.where``).
_ScopedStmt = TypeVar("_ScopedStmt", Select, Update, Delete)

#: Column names, in priority order, that conventionally carry the tenant key.
_TENANT_COLUMN_CANDIDATES: tuple[str, ...] = (
    "tenant_key",
    "tenant_id",
    "workspace_id",
    "org_id",
)


class UnscopedQueryError(RuntimeError):
    """Raised when a tenant-scoped query carries no tenant filter.

    The verifier's fail-closed signal: a query over a tenant-owned entity that
    does not constrain the tenant column is rejected rather than executed.
    """


class MissingTenantColumnError(RuntimeError):
    """Raised when an entity declared tenant-scoped exposes no tenant column."""


def tenant_column_name(entity: Any) -> str:
    """Resolve the tenant-key column name for ``entity``.

    Honors an explicit ``__tenant_column__`` override, else picks the first
    conventional candidate the entity actually exposes. Raises
    :class:`MissingTenantColumnError` if none is found — a model that wants the
    guard must have a tenant column.
    """
    override = getattr(entity, "__tenant_column__", None)
    if override is not None:
        if not _has_column(entity, override):
            raise MissingTenantColumnError(
                f"{entity!r} declares __tenant_column__={override!r} but has no such column"
            )
        return str(override)
    for name in _TENANT_COLUMN_CANDIDATES:
        if _has_column(entity, name):
            return name
    raise MissingTenantColumnError(
        f"{entity!r} has no tenant column (looked for {_TENANT_COLUMN_CANDIDATES})"
    )


def _has_column(entity: Any, name: str) -> bool:
    """Whether ``entity`` (ORM class or Core table) exposes a column ``name``.

    Accepts both the ORM :class:`QueryableAttribute` (an
    ``InstrumentedAttribute`` is *not* a ``ColumnElement`` but is column-like) and
    a Core :class:`ColumnElement`.
    """
    col = getattr(entity, name, None)
    return isinstance(col, (QueryableAttribute, ColumnElement))


def _tenant_value(ctx: TenantContext | None) -> str:
    """The tenant key to filter on (explicit ctx or the active one)."""
    resolved = ctx if ctx is not None else require_tenant()
    return resolved.tenant_key


def tenant_filter(entity: Any, ctx: TenantContext | None = None) -> ColumnElement[bool]:
    """The ``entity.<tenant_column> == <tenant_key>`` predicate for the context."""
    column = getattr(entity, tenant_column_name(entity))
    return column == _tenant_value(ctx)


def tenant_scoped(
    stmt: _ScopedStmt,
    entity: Any,
    ctx: TenantContext | None = None,
) -> _ScopedStmt:
    """Append the active tenant's filter to ``stmt`` over ``entity``.

    The one safe way to query a tenant-owned table: the caller writes the
    business predicate and this adds the tenant predicate. Works for ``select``,
    ``update`` and ``delete``. Raises
    :class:`~app.tenancy.context.NoTenantContext` if no tenant is resolved (fail
    closed — there is no "all tenants" mode here).
    """
    predicate = tenant_filter(entity, ctx)
    return stmt.where(predicate)


def _whereclause(stmt: Select | Update | Delete) -> ColumnElement[Any] | None:
    """The WHERE clause of a statement, or ``None`` if it has none."""
    return stmt.whereclause


def _iter_predicates(clause: ColumnElement[Any] | None) -> list[ColumnElement[Any]]:
    """Flatten a (possibly AND-nested) WHERE clause into its leaf predicates."""
    if clause is None:
        return []
    if isinstance(clause, BooleanClauseList) and clause.operator is operators.and_:
        out: list[ColumnElement[Any]] = []
        for child in clause.clauses:
            out.extend(_iter_predicates(child))
        return out
    return [clause]


def _constrains_tenant_column(clause: ColumnElement[Any] | None, column_name: str) -> bool:
    """Whether any top-level AND predicate is an equality on ``column_name``.

    Only an equality binding (``col == value``) counts as a tenant scope; an
    ``IN`` over many tenants or a ``!=`` would not isolate, so they are rejected.
    The predicate must also sit at the top level of the WHERE conjunction — a
    tenant filter buried inside an ``OR`` does not isolate and is not credited.
    """
    for predicate in _iter_predicates(clause):
        if not isinstance(predicate, BinaryExpression):
            continue
        if predicate.operator is not operators.eq:
            continue
        left = getattr(predicate, "left", None)
        if left is not None and getattr(left, "key", None) == column_name:
            return True
    return False


def assert_scoped(stmt: Select | Update | Delete, entity: Any) -> None:
    """Raise :class:`UnscopedQueryError` unless ``stmt`` filters ``entity``'s tenant.

    The verifier half of the guard: call it on any hand-written statement over a
    tenant-owned entity before executing it. Passes silently when the statement
    is properly scoped (typically because it went through :func:`tenant_scoped`).
    """
    column_name = tenant_column_name(entity)
    if not _constrains_tenant_column(_whereclause(stmt), column_name):
        raise UnscopedQueryError(
            f"query over {getattr(entity, '__name__', entity)!r} is missing a "
            f"top-level equality filter on tenant column {column_name!r}"
        )


def guard_select(
    stmt: _ScopedStmt,
    entity: Any,
    ctx: TenantContext | None = None,
) -> _ScopedStmt:
    """Scope ``stmt`` and re-verify it — the recommended one-call wrapper.

    Equivalent to :func:`tenant_scoped` followed by :func:`assert_scoped`, so the
    returned statement is guaranteed both built-with and verified-to-carry the
    tenant filter. Use this at the repository boundary.
    """
    scoped = tenant_scoped(stmt, entity, ctx)
    assert_scoped(scoped, entity)
    return scoped


def is_visible(row_tenant_key: str | None, ctx: TenantContext | None = None) -> bool:
    """Whether a row with ``row_tenant_key`` is visible to the active tenant.

    A row-level companion to the query guard for code paths that already hold a
    fetched row (e.g. an object-store listing) and want to fail-close on a
    cross-tenant value before returning it.
    """
    return row_tenant_key == _tenant_value(ctx)


def merge_filters(*predicates: ColumnElement[bool]) -> ColumnElement[bool]:
    """AND a tenant predicate with business predicates (small convenience)."""
    return and_(*predicates)


__all__ = [
    "MissingTenantColumnError",
    "UnscopedQueryError",
    "assert_scoped",
    "guard_select",
    "is_visible",
    "merge_filters",
    "tenant_column_name",
    "tenant_filter",
    "tenant_scoped",
]
