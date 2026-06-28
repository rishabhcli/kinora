"""Unit tests for the pure RBAC policy in :mod:`app.auth.rbac` (no infra)."""

from __future__ import annotations

from app.auth import rbac
from app.auth.rbac import Principal


def test_has_capability_exact_and_wildcards() -> None:
    held = {"books:read", "library:*"}
    assert rbac.has_capability(held, "books:read")
    assert rbac.has_capability(held, "library:write")  # resource wildcard
    assert not rbac.has_capability(held, "books:write")
    assert rbac.has_capability({"*"}, "anything:at:all")  # global wildcard


def test_expand_roles_to_permissions() -> None:
    perms = rbac.expand_roles_to_permissions(["reader"])
    assert "books:read" in perms
    assert "books:write" not in perms
    editor = rbac.expand_roles_to_permissions(["editor"])
    assert "books:write" in editor


def test_normalize_scopes_drops_unknown_keeps_wildcards() -> None:
    out = rbac.normalize_scopes(["books:read", "nonsense:do", "library:*", "*", "books:read"])
    assert "books:read" in out
    assert "library:*" in out
    assert "*" in out
    assert "nonsense:do" not in out  # unknown concrete scope dropped
    assert out.count("books:read") == 1  # de-duplicated


def test_principal_permission_checks() -> None:
    p = Principal(
        user_id="u1",
        permissions=frozenset({"books:read", "library:read"}),
        roles=frozenset({"reader"}),
    )
    assert p.has_permission("books:read")
    assert not p.has_permission("books:write")
    assert p.has_any(["books:write", "books:read"])
    assert not p.has_all(["books:read", "books:write"])
    assert p.has_role("reader")
    assert not p.has_role("admin")


def test_admin_principal_has_everything() -> None:
    admin = Principal(user_id="root", permissions=frozenset({"*"}), roles=frozenset({"admin"}))
    assert admin.has_permission("admin:rbac")
    assert admin.has_permission("books:delete")
    assert admin.can_access_tenant("any-tenant")  # admin crosses tenants


def test_tenant_isolation() -> None:
    scoped = Principal(user_id="u", permissions=frozenset({"books:read"}), tenant_id="acme")
    assert scoped.can_access_tenant("acme")
    assert not scoped.can_access_tenant("globex")
    assert not scoped.can_access_tenant(None)
    glob = Principal(user_id="u2", permissions=frozenset({"books:read"}), tenant_id=None)
    assert glob.can_access_tenant(None)
    assert not glob.can_access_tenant("acme")


def test_api_key_principal_flag() -> None:
    key_p = Principal(user_id="u", permissions=frozenset({"books:read"}), api_key_id="key-1")
    assert key_p.is_api_key
    user_p = Principal(user_id="u", permissions=frozenset({"books:read"}))
    assert not user_p.is_api_key


def test_catalogue_is_self_consistent() -> None:
    """Every concrete permission a built-in role grants exists in the catalogue."""
    for role, perms in rbac.ROLES.items():
        for perm in perms:
            if perm == rbac.WILDCARD:
                continue
            assert perm in rbac.PERMISSIONS, f"{role} grants undefined permission {perm}"
    assert rbac.DEFAULT_ROLE in rbac.ROLES
