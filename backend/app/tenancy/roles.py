"""Org/workspace RBAC: the role lattice, permission catalogue, and ``require``.

This is the **pure vocabulary + policy** of the multi-tenant authorization model
and is deliberately free of any I/O or ORM dependency so the permission check
(:func:`app.tenancy.policy.require` / ``check``) stays an exhaustively
unit-testable decision function (kinora.md §6 — the RBAC/scope model mirrored at
the *tenant* granularity rather than the global-account granularity).

The role model is a small **totally-ordered lattice** scoped to a tenant
(an organization or one of its workspaces)::

    OWNER  >  ADMIN  >  EDITOR  >  VIEWER

Each role grants a *set* of :class:`Permission` capabilities; a higher role is a
strict superset of every lower role's capabilities (the lattice order and the
capability-subset order agree — verified by a test). When a principal holds
several applicable roles (an org role *and* a workspace role) the most-permissive
wins — see :func:`effective_role`.

Why a second role enum at all (the workspaces subsystem already has one)? This
namespace owns the *isolation* concern: a permission is only ever evaluated
inside a resolved :class:`~app.tenancy.context.TenantContext`, and ``require``
fail-closes both on the missing capability **and** on a tenant mismatch. The
role names here are the four canonical platform roles named by the task contract
(owner/admin/editor/viewer) and are independent of any other subsystem's lattice.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from typing import Final

# --------------------------------------------------------------------------- #
# The role lattice
# --------------------------------------------------------------------------- #


class Role(enum.StrEnum):
    """A membership role within a tenant (org or workspace).

    Ordered most-privileged → least. Use :func:`role_rank` / :func:`role_at_least`
    rather than comparing the string values directly.
    """

    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


#: Rank in the lattice — higher number == more privilege.
_ROLE_RANK: Final[dict[Role, int]] = {
    Role.VIEWER: 0,
    Role.EDITOR: 1,
    Role.ADMIN: 2,
    Role.OWNER: 3,
}


def role_rank(role: Role) -> int:
    """Return the lattice rank of ``role`` (higher == more privileged)."""
    return _ROLE_RANK[role]


def role_at_least(role: Role | None, floor: Role) -> bool:
    """Whether ``role`` is at least as privileged as ``floor`` (``None`` -> False)."""
    if role is None:
        return False
    return _ROLE_RANK[role] >= _ROLE_RANK[floor]


def effective_role(*roles: Role | None) -> Role | None:
    """Return the most-permissive of ``roles`` (ignoring ``None``), or ``None``.

    This collapses an org-level role and a workspace-level role into one
    effective role: the strongest one wins.
    """
    present = [r for r in roles if r is not None]
    if not present:
        return None
    return max(present, key=role_rank)


# --------------------------------------------------------------------------- #
# The permission catalogue
# --------------------------------------------------------------------------- #


class Permission(enum.StrEnum):
    """A fine-grained capability evaluated against a principal's effective role.

    Strings use the same ``resource:action`` vocabulary as the account-level RBAC
    so a wildcard (``*`` / ``resource:*``) check is uniform across both worlds.
    """

    # Reading / library
    BOOK_READ = "book:read"
    BOOK_WRITE = "book:write"
    BOOK_DELETE = "book:delete"
    SESSION_READ = "session:read"
    SESSION_WRITE = "session:write"
    # Creative / spend
    RENDER_ENQUEUE = "render:enqueue"
    DIRECTOR_COMMENT = "director:comment"
    # Membership / tenant administration
    MEMBER_READ = "member:read"
    MEMBER_INVITE = "member:invite"
    MEMBER_REMOVE = "member:remove"
    MEMBER_ROLE_SET = "member:role_set"
    WORKSPACE_CREATE = "workspace:create"
    WORKSPACE_DELETE = "workspace:delete"
    SETTINGS_READ = "settings:read"
    SETTINGS_WRITE = "settings:write"
    QUOTA_MANAGE = "quota:manage"
    BILLING_MANAGE = "billing:manage"
    ORG_DELETE = "org:delete"


#: The wildcard the OWNER role carries (matches every permission).
WILDCARD: Final = "*"


#: Each role → the set of permissions it grants. A higher role is a strict
#: superset of every lower role's set (OWNER's full set trivially so). The
#: superset invariant is asserted by a test, not just by convention.
_VIEWER_PERMS: frozenset[Permission] = frozenset(
    {
        Permission.BOOK_READ,
        Permission.SESSION_READ,
        Permission.MEMBER_READ,
        Permission.SETTINGS_READ,
    }
)
_EDITOR_PERMS: frozenset[Permission] = _VIEWER_PERMS | {
    Permission.BOOK_WRITE,
    Permission.SESSION_WRITE,
    Permission.RENDER_ENQUEUE,
    Permission.DIRECTOR_COMMENT,
}
_ADMIN_PERMS: frozenset[Permission] = _EDITOR_PERMS | {
    Permission.BOOK_DELETE,
    Permission.MEMBER_INVITE,
    Permission.MEMBER_REMOVE,
    Permission.MEMBER_ROLE_SET,
    Permission.WORKSPACE_CREATE,
    Permission.WORKSPACE_DELETE,
    Permission.SETTINGS_WRITE,
    Permission.QUOTA_MANAGE,
}

ROLE_PERMISSIONS: Final[dict[Role, frozenset[Permission]]] = {
    Role.VIEWER: _VIEWER_PERMS,
    Role.EDITOR: _EDITOR_PERMS,
    Role.ADMIN: _ADMIN_PERMS,
    # OWNER gets the full catalogue so it implicitly gains any future permission
    # (billing, org-delete, …) without editing this table.
    Role.OWNER: frozenset(Permission),
}


def role_permissions(role: Role | None) -> frozenset[Permission]:
    """The permission set a role grants (empty for ``None``)."""
    if role is None:
        return frozenset()
    return ROLE_PERMISSIONS.get(role, frozenset())


def role_grants(role: Role | None, permission: Permission) -> bool:
    """Whether ``role`` grants ``permission`` (OWNER grants everything)."""
    if role is None:
        return False
    if role is Role.OWNER:
        return True
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def has_capability(held: Iterable[str], required: str) -> bool:
    """Whether a string capability set ``held`` grants ``required``.

    Supports ``*`` and ``resource:*`` wildcards, mirroring
    :func:`app.auth.rbac.has_capability` so the two authorization worlds share a
    single matching rule. Provided for principals whose capabilities come from an
    API key's scopes rather than a role.
    """
    held_set = set(held)
    if WILDCARD in held_set:
        return True
    if required in held_set:
        return True
    resource = required.split(":", 1)[0]
    return f"{resource}:{WILDCARD}" in held_set


__all__ = [
    "ROLE_PERMISSIONS",
    "WILDCARD",
    "Permission",
    "Role",
    "effective_role",
    "has_capability",
    "role_at_least",
    "role_grants",
    "role_permissions",
    "role_rank",
]
