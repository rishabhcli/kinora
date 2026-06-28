"""RBAC catalogue, scope model, and per-tenant authorisation logic (kinora.md §6).

Two related-but-distinct concepts:

* **Permissions** — fine-grained capabilities (``books:write``, ``admin:rbac``)
  bundled into **roles** (``reader``, ``editor``, ``admin``) which users hold via
  **role bindings**, optionally scoped to a tenant. This is the interactive
  (human) authorisation model.
* **Scopes** — the capability set carried by an **API key**; a headless caller is
  limited to the intersection of its key's scopes and (optionally) the owner's
  permissions. Scopes use the same ``resource:action`` string vocabulary as
  permissions so a single ``has_capability`` check covers both worlds.

This module owns the *pure* policy (the built-in catalogue, wildcard matching,
tenant-scoping rules); the stateful seeding/granting lives in
:class:`app.auth.repositories.RbacRepo` and :class:`app.auth.service.AuthService`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# The built-in permission + role catalogue
# --------------------------------------------------------------------------- #

#: Every built-in fine-grained permission (resource:action).
PERMISSIONS: dict[str, str] = {
    "books:read": "View books and pages",
    "books:write": "Upload/import and edit books",
    "books:delete": "Delete books",
    "library:read": "Browse the library shelf",
    "sessions:read": "View reading sessions",
    "sessions:write": "Drive reading sessions (scroll/seek/intent)",
    "director:comment": "Issue director region-comments and canon edits",
    "render:enqueue": "Enqueue render jobs",
    "prefs:read": "Read learned directing preferences",
    "prefs:write": "Reset/override directing preferences",
    "metrics:read": "Read session/render metrics",
    "apikeys:manage": "Create and revoke API keys",
    "mfa:manage": "Enrol/disable MFA on own account",
    "admin:rbac": "Manage roles, permissions, and bindings",
    "admin:users": "Disable/enable accounts and impersonate",
    "admin:audit": "Read the security audit log of any user",
}

#: The wildcard permission an ``admin`` role holds (matches everything).
WILDCARD = "*"

#: Built-in roles → the permissions they grant.
ROLES: dict[str, list[str]] = {
    "reader": [
        "books:read",
        "library:read",
        "sessions:read",
        "sessions:write",
        "prefs:read",
        "metrics:read",
        "mfa:manage",
        "apikeys:manage",
    ],
    "editor": [
        "books:read",
        "books:write",
        "library:read",
        "sessions:read",
        "sessions:write",
        "director:comment",
        "render:enqueue",
        "prefs:read",
        "prefs:write",
        "metrics:read",
        "mfa:manage",
        "apikeys:manage",
    ],
    "admin": [WILDCARD],
}

#: The role assigned to a freshly-registered account.
DEFAULT_ROLE = "reader"

#: Roles that the admin API cannot delete/rename (the built-ins).
SYSTEM_ROLES = frozenset(ROLES)


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller's effective authorisation context.

    Produced by the route dependency from either an access token (interactive
    user) or an API key (headless caller). The permission checks below operate
    purely on this value, so they are framework-agnostic and unit-testable.
    """

    user_id: str
    #: Effective permission names (from roles, or an API key's scopes).
    permissions: frozenset[str] = frozenset()
    #: Role names the user holds (empty for an API-key principal).
    roles: frozenset[str] = frozenset()
    #: The tenant the caller is acting within (per-tenant isolation).
    tenant_id: str | None = None
    #: The auth session id (``sid``) if this came from an access token.
    session_id: str | None = None
    #: The API-key row id when the principal authenticated with a key.
    api_key_id: str | None = None

    @property
    def is_api_key(self) -> bool:
        """Whether this principal authenticated via an API key."""
        return self.api_key_id is not None

    def has_permission(self, permission: str) -> bool:
        """Whether the principal holds ``permission`` (``*`` matches everything)."""
        return has_capability(self.permissions, permission)

    def has_any(self, permissions: Iterable[str]) -> bool:
        """Whether the principal holds at least one of ``permissions``."""
        return any(self.has_permission(p) for p in permissions)

    def has_all(self, permissions: Iterable[str]) -> bool:
        """Whether the principal holds every one of ``permissions``."""
        return all(self.has_permission(p) for p in permissions)

    def has_role(self, role: str) -> bool:
        """Whether the principal holds ``role``."""
        return role in self.roles

    def can_access_tenant(self, tenant_id: str | None) -> bool:
        """Per-tenant isolation: a caller may only touch its own tenant's data.

        * A global principal (no tenant) can access global resources (no tenant).
        * A tenant-scoped principal can only access matching-tenant resources.
        * An ``admin`` (wildcard) principal crosses tenant boundaries.
        """
        if self.has_permission("admin:users") or WILDCARD in self.permissions:
            return True
        return self.tenant_id == tenant_id


def has_capability(held: Iterable[str], required: str) -> bool:
    """Whether ``held`` grants ``required`` (supports ``*`` and ``resource:*``).

    Matching rules (most → least powerful):
      * ``*``            — grants everything.
      * ``resource:*``   — grants every action on ``resource``.
      * exact match.
    """
    held_set = set(held)
    if WILDCARD in held_set:
        return True
    if required in held_set:
        return True
    resource = required.split(":", 1)[0]
    return f"{resource}:{WILDCARD}" in held_set


def role_permissions(role: str) -> list[str]:
    """The built-in permissions for a role (empty for an unknown role)."""
    return list(ROLES.get(role, []))


def expand_roles_to_permissions(roles: Iterable[str]) -> frozenset[str]:
    """The union of built-in permissions for a set of role names."""
    out: set[str] = set()
    for role in roles:
        out.update(role_permissions(role))
    return frozenset(out)


def normalize_scopes(scopes: Iterable[str]) -> list[str]:
    """Validate + de-duplicate requested API-key scopes against the catalogue.

    ``*`` and ``resource:*`` wildcards are accepted; an unknown concrete scope is
    dropped (an API key can never be granted a capability that doesn't exist).
    """
    valid: list[str] = []
    seen: set[str] = set()
    for raw in scopes:
        scope = raw.strip().lower()
        if not scope or scope in seen:
            continue
        if scope == WILDCARD or scope.endswith(f":{WILDCARD}") or scope in PERMISSIONS:
            valid.append(scope)
            seen.add(scope)
    return valid


__all__ = [
    "DEFAULT_ROLE",
    "PERMISSIONS",
    "ROLES",
    "SYSTEM_ROLES",
    "WILDCARD",
    "Principal",
    "expand_roles_to_permissions",
    "has_capability",
    "normalize_scopes",
    "role_permissions",
]
