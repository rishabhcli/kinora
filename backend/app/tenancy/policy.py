"""The RBAC decision functions: ``require(permission, ctx)`` and friends.

These are the stateful entry points the API layer calls. They wrap the pure role
lattice (:mod:`app.tenancy.roles`) with two fail-closed guards specific to
multi-tenancy:

1. **No context → deny.** A permission check with no resolved
   :class:`~app.tenancy.context.TenantContext` raises rather than defaulting to
   allow — an un-tenanted caller can never act on tenant-scoped resources.
2. **Tenant mismatch → deny.** When a check names a target tenant
   (``within=``), the active context must match it; otherwise a member of org A
   could exercise a permission they legitimately hold *in org A* against a
   resource in org B. This is the authorization complement to the query guard's
   data-level isolation.

``check`` returns a bool; ``require`` raises :class:`PermissionDenied`. Both
read the active context from the context var when one is not passed explicitly,
so route code can simply write ``require(Permission.BOOK_WRITE)``.
"""

from __future__ import annotations

from app.tenancy.context import (
    NoTenantContext,
    TenantContext,
    current_tenant,
)
from app.tenancy.roles import Permission


class PermissionDenied(PermissionError):  # noqa: N818 - public contract name
    """Raised by :func:`require` when the context lacks a permission.

    Carries the structured triple (permission, the acting tenant key, the
    required-against tenant key) so the API layer can turn it into a typed 403
    without re-deriving why.
    """

    def __init__(
        self,
        permission: Permission,
        *,
        tenant_key: str | None,
        target_tenant_key: str | None = None,
        reason: str = "insufficient_role",
    ) -> None:
        self.permission = permission
        self.tenant_key = tenant_key
        self.target_tenant_key = target_tenant_key
        self.reason = reason
        super().__init__(
            f"permission denied: {permission} ({reason}) "
            f"for tenant {tenant_key!r}"
            + (f" against {target_tenant_key!r}" if target_tenant_key else "")
        )


def _resolve(ctx: TenantContext | None) -> TenantContext:
    """Return ``ctx`` or the active context, raising if neither exists."""
    resolved = ctx if ctx is not None else current_tenant()
    if resolved is None:
        raise NoTenantContext("permission check with no tenant context bound")
    return resolved


def check(
    permission: Permission,
    ctx: TenantContext | None = None,
    *,
    within: str | None = None,
) -> bool:
    """Whether ``ctx`` (or the active context) grants ``permission``.

    ``within`` optionally pins the target tenant key; a mismatch returns ``False``
    even when the role would otherwise grant the permission (cross-tenant deny).

    Raises :class:`~app.tenancy.context.NoTenantContext` only when there is *no*
    context at all — a present-but-insufficient context returns ``False``.
    """
    resolved = _resolve(ctx)
    if within is not None and within != resolved.tenant_key:
        return False
    return resolved.grants(permission)


def require(
    permission: Permission,
    ctx: TenantContext | None = None,
    *,
    within: str | None = None,
) -> TenantContext:
    """Assert ``ctx`` grants ``permission``; raise :class:`PermissionDenied` else.

    Returns the resolved context on success so a caller can chain
    ``ctx = require(Permission.BOOK_WRITE)``. ``within`` enforces the
    cross-tenant deny (see :func:`check`).
    """
    resolved = _resolve(ctx)
    if within is not None and within != resolved.tenant_key:
        raise PermissionDenied(
            permission,
            tenant_key=resolved.tenant_key,
            target_tenant_key=within,
            reason="tenant_mismatch",
        )
    if not resolved.grants(permission):
        raise PermissionDenied(
            permission,
            tenant_key=resolved.tenant_key,
            reason="insufficient_role",
        )
    return resolved


def require_any(
    *permissions: Permission,
    ctx: TenantContext | None = None,
    within: str | None = None,
) -> TenantContext:
    """Assert the context grants at least one of ``permissions``."""
    resolved = _resolve(ctx)
    if within is not None and within != resolved.tenant_key:
        raise PermissionDenied(
            permissions[0],
            tenant_key=resolved.tenant_key,
            target_tenant_key=within,
            reason="tenant_mismatch",
        )
    if any(resolved.grants(p) for p in permissions):
        return resolved
    raise PermissionDenied(
        permissions[0],
        tenant_key=resolved.tenant_key,
        reason="insufficient_role",
    )


__all__ = [
    "PermissionDenied",
    "check",
    "require",
    "require_any",
]
