"""Unit tests for the plane RBAC + ABAC engines (no infra)."""

from __future__ import annotations

from app.platform.authz.abac import (
    AbacEffect,
    AbacEngine,
    AbacRule,
    AllOf,
    AnyOf,
    Attr,
    Not,
    action_matches,
    is_owner,
    resolve_attr,
    same_tenant,
)
from app.platform.authz.model import (
    AuthorizationRequest,
    Context,
    Effect,
    Resource,
    Subject,
)
from app.platform.authz.rbac import RbacEngine, RoleCatalogue, permission_matches


def _req(
    action: str,
    *,
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
    subject_id: str = "alice",
    subject_tenant: str | None = None,
    resource_owner: str | None = None,
    resource_tenant: str | None = None,
    is_admin: bool = False,
    ctx: dict | None = None,
) -> AuthorizationRequest:
    sattrs: dict = {}
    if roles is not None:
        sattrs["roles"] = roles
    if permissions is not None:
        sattrs["permissions"] = permissions
    if subject_tenant is not None:
        sattrs["tenant"] = subject_tenant
    if is_admin:
        sattrs["is_admin"] = True
    rattrs: dict = {}
    if resource_owner is not None:
        rattrs["owner"] = resource_owner
    if resource_tenant is not None:
        rattrs["tenant"] = resource_tenant
    return AuthorizationRequest(
        subject=Subject(type="user", id=subject_id, attributes=sattrs),
        action=action,
        resource=Resource(type="book", id="42", attributes=rattrs),
        context=Context(attributes=ctx or {}),
    )


# -- permission matching (parity with auth.rbac.has_capability) -------------- #


def test_permission_matches_parity() -> None:
    held = {"book:read", "library:*"}
    assert permission_matches(held, "book:read")
    assert permission_matches(held, "library:write")
    assert not permission_matches(held, "book:write")
    assert permission_matches({"*"}, "anything:goes")


def test_rbac_role_catalogue_from_auth_matches_legacy() -> None:
    from app.auth.rbac import ROLES, expand_roles_to_permissions

    cat = RoleCatalogue.from_auth_catalogue()
    assert cat.role_names == frozenset(ROLES)
    assert cat.expand(["editor"]) == expand_roles_to_permissions(["editor"])


def test_rbac_engine_allows_via_role() -> None:
    cat = RoleCatalogue({"editor": ["book:write", "book:read"]})
    engine = RbacEngine(cat)
    result = engine.evaluate(_req("book:write", roles=["editor"]))
    assert result.effect is Effect.ALLOW
    assert "book:write" in result.reasons[0].detail


def test_rbac_engine_abstains_without_grant() -> None:
    cat = RoleCatalogue({"reader": ["book:read"]})
    engine = RbacEngine(cat)
    assert engine.evaluate(_req("book:write", roles=["reader"])).effect is Effect.ABSTAIN
    # no roles at all → abstain (no opinion, not deny)
    assert engine.evaluate(_req("book:read")).effect is Effect.ABSTAIN


def test_rbac_engine_direct_permissions_bypass_roles() -> None:
    cat = RoleCatalogue({})
    engine = RbacEngine(cat)
    # an API-key style principal carries scopes in `permissions`
    result = engine.evaluate(_req("render:enqueue", permissions=["render:*"]))
    assert result.effect is Effect.ALLOW


def test_rbac_engine_wildcard_admin() -> None:
    cat = RoleCatalogue({"admin": ["*"]})
    engine = RbacEngine(cat)
    assert engine.evaluate(_req("anything:weird", roles=["admin"])).effect is Effect.ALLOW


# -- ABAC attribute resolution ----------------------------------------------- #


def test_resolve_attr_roots() -> None:
    req = _req("book:read", subject_id="bob", resource_owner="bob", ctx={"ip": "x"})
    assert resolve_attr(req, "subject.id") == "bob"
    assert resolve_attr(req, "subject.type") == "user"
    assert resolve_attr(req, "resource.owner") == "bob"
    assert resolve_attr(req, "resource.id") == "42"
    assert resolve_attr(req, "context.ip") == "x"
    assert resolve_attr(req, "action") == "book:read"
    assert resolve_attr(req, "subject.missing") is None
    assert resolve_attr(req, "nonsense") is None


# -- ABAC condition algebra -------------------------------------------------- #


def test_attr_comparisons() -> None:
    req = _req("book:read", ctx={"speed": 5})
    assert Attr("context.speed", "eq", 5).holds(req)
    assert Attr("context.speed", "gt", 3).holds(req)
    assert not Attr("context.speed", "lt", 3).holds(req)
    assert Attr("context.missing", "ne", "x").holds(req)  # None != "x"
    assert not Attr("context.missing", "gt", 3).holds(req)  # None comparisons are False


def test_attr_in_and_contains() -> None:
    req = _req("book:read", roles=["editor", "reader"])
    assert Attr("subject.roles", "contains", "editor").holds(req)
    assert Attr("action", "in", "book:read,book:write").holds(req)  # substring of a str


def test_attr_eq_attr_ownership_and_tenant() -> None:
    owned = _req("book:edit", subject_id="x", resource_owner="x")
    assert is_owner().holds(owned)
    not_owned = _req("book:edit", subject_id="x", resource_owner="y")
    assert not is_owner().holds(not_owned)
    same = _req("book:read", subject_tenant="t1", resource_tenant="t1")
    assert same_tenant().holds(same)
    diff = _req("book:read", subject_tenant="t1", resource_tenant="t2")
    assert not same_tenant().holds(diff)


def test_boolean_combinators() -> None:
    req = _req(
        "book:edit",
        subject_id="x",
        resource_owner="x",
        subject_tenant="t1",
        resource_tenant="t1",
    )
    assert (is_owner() & same_tenant()).holds(req)
    assert (is_owner() | Attr("subject.id", "eq", "nobody")).holds(req)
    assert AllOf([is_owner(), same_tenant()]).holds(req)
    assert AnyOf([Attr("subject.id", "eq", "no"), is_owner()]).holds(req)
    assert Not(Attr("subject.id", "eq", "no")).holds(req)
    assert (~Attr("subject.id", "eq", "x")).holds(req) is False


def test_action_matches_patterns() -> None:
    assert action_matches("*", "book:read")
    assert action_matches("book:read", "book:read")
    assert action_matches("book:*", "book:write")
    assert not action_matches("book:*", "workspace:read")


# -- ABAC engine: first-applicable + deny short-circuit ---------------------- #


def test_abac_engine_owner_allow() -> None:
    rule = AbacRule(
        name="owner",
        actions=frozenset({"book:*"}),
        condition=is_owner(),
        effect=AbacEffect.ALLOW,
    )
    engine = AbacEngine([rule])
    res = engine.evaluate(_req("book:edit", subject_id="x", resource_owner="x"))
    assert res.effect is Effect.ALLOW and res.reasons[0].rule == "owner"


def test_abac_engine_deny_short_circuits() -> None:
    deny = AbacRule(
        name="block-suspended",
        actions=frozenset({"*"}),
        condition=Attr("subject.suspended", "eq", True),
        effect=AbacEffect.DENY,
    )
    allow = AbacRule(
        name="owner",
        actions=frozenset({"book:*"}),
        condition=is_owner(),
        effect=AbacEffect.ALLOW,
    )
    engine = AbacEngine([deny, allow])
    req = AuthorizationRequest(
        subject=Subject(type="user", id="x", attributes={"suspended": True}),
        action="book:edit",
        resource=Resource(type="book", id="42", attributes={"owner": "x"}),
    )
    res = engine.evaluate(req)
    assert res.effect is Effect.DENY and res.reasons[0].rule == "block-suspended"


def test_abac_engine_abstains_when_no_rule_applies() -> None:
    engine = AbacEngine([
        AbacRule(name="r", actions=frozenset({"workspace:edit"}), condition=is_owner())
    ])
    assert engine.evaluate(_req("book:read")).effect is Effect.ABSTAIN
