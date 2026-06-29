"""Kinora's authorization presets — the namespaces, roles, and action map.

The plane is generic; this module is where Kinora's *specific* model lives, so
the rest of the codebase can build a fully-wired plane with one call. It encodes
the same structure the scattered checks already use, expressed once:

* the **Zanzibar namespaces** for ``book`` / ``workspace`` / ``collection`` —
  mirroring :mod:`app.workspaces.roles` (OWNER ⊃ EDITOR ⊃ COMMENTER ⊃ VIEWER)
  and the inheritance paths in :mod:`app.workspaces.authz` (a book's viewer
  includes the member of its parent workspace; the workspace's owner is the org
  owner). The role lattice becomes ``computed_userset`` chains; the
  book→workspace and workspace→org paths become ``tuple_to_userset`` edges;
* the **action→relation map** translating product verbs (``book:view``,
  ``book:edit``, ``book:comment``, ``book:render``) to the relation that grants
  them;
* the **RBAC role catalogue** (re-exported from :mod:`app.auth.rbac`) so the
  plane's RBAC engine speaks the same permission vocabulary as the auth package;
* a small set of **default ABAC rules** capturing the invariants the legacy
  resolvers enforce in code: the personal owner can never be locked out, tenant
  isolation, and an admin wildcard.

These presets are the bridge that lets the adapters fold the existing checks in
without changing behaviour: the namespaces reproduce the workspaces lattice, the
RBAC catalogue reproduces the auth catalogue, and the ABAC rules reproduce the
hard-coded invariants.
"""

from __future__ import annotations

from app.platform.authz.abac import (
    AbacEffect,
    AbacRule,
    AllOf,
    Attr,
    is_owner,
    same_tenant,
)
from app.platform.authz.rbac import RoleCatalogue
from app.platform.authz.rebac import (
    ComputedUserset,
    Namespace,
    This,
    TupleToUserset,
    Union,
)

# --------------------------------------------------------------------------- #
# Zanzibar namespaces — the workspaces role lattice as userset rewrites
# --------------------------------------------------------------------------- #

#: The ``workspace`` namespace. A member's role is stored directly; the lattice
#: (owner ⊇ editor ⊇ commenter ⊇ viewer) is expressed as computed-userset
#: inheritance, and the org owner is folded in via the ``org`` parent edge.
WORKSPACE_NAMESPACE = Namespace(
    type="workspace",
    relations={
        # The strongest role: a direct owner tuple, OR the owner of the parent org.
        "owner": Union(
            (
                This(),
                TupleToUserset(tupleset="org", computed="owner"),
            )
        ),
        # Each lower role includes everyone with the role above it.
        "editor": Union((This(), ComputedUserset("owner"))),
        "commenter": Union((This(), ComputedUserset("editor"))),
        "viewer": Union((This(), ComputedUserset("commenter"))),
        # `member` is the union of every role-holder (any membership at all).
        "member": Union((This(), ComputedUserset("viewer"))),
    },
)

#: The ``book`` namespace. The personal owner is a direct ``owner`` tuple; a book
#: attached to a workspace inherits that workspace's roles via the ``parent``
#: edge (book#parent@workspace:7 ⇒ book viewers include workspace viewers, etc.).
BOOK_NAMESPACE = Namespace(
    type="book",
    relations={
        "owner": Union(
            (
                This(),
                TupleToUserset(tupleset="parent", computed="owner"),
            )
        ),
        "editor": Union(
            (
                This(),
                ComputedUserset("owner"),
                TupleToUserset(tupleset="parent", computed="editor"),
            )
        ),
        "commenter": Union(
            (
                This(),
                ComputedUserset("editor"),
                TupleToUserset(tupleset="parent", computed="commenter"),
            )
        ),
        "viewer": Union(
            (
                This(),
                ComputedUserset("commenter"),
                TupleToUserset(tupleset="parent", computed="viewer"),
            )
        ),
    },
)

#: The ``collection`` namespace — a collection lives in a workspace and inherits
#: its roles via the ``parent`` edge (collection#parent@workspace:7).
COLLECTION_NAMESPACE = Namespace(
    type="collection",
    relations={
        "owner": TupleToUserset(tupleset="parent", computed="owner"),
        "editor": Union(
            (
                ComputedUserset("owner"),
                TupleToUserset(tupleset="parent", computed="editor"),
            )
        ),
        "commenter": Union(
            (
                ComputedUserset("editor"),
                TupleToUserset(tupleset="parent", computed="commenter"),
            )
        ),
        "viewer": Union(
            (
                ComputedUserset("commenter"),
                TupleToUserset(tupleset="parent", computed="viewer"),
            )
        ),
    },
)

KINORA_NAMESPACES = (WORKSPACE_NAMESPACE, BOOK_NAMESPACE, COLLECTION_NAMESPACE)


# --------------------------------------------------------------------------- #
# Action → relation map (product verbs → the relation that grants them)
# --------------------------------------------------------------------------- #

#: Map of plane actions to the Zanzibar relation that grants them. Mirrors the
#: capability lattice in :mod:`app.workspaces.roles` (viewer→view, commenter→
#: comment, editor→edit/render/download, owner→share/manage/delete).
ACTION_RELATION: dict[str, str] = {
    # content verbs
    "book:view": "viewer",
    "book:read": "viewer",
    "book:comment": "commenter",
    "book:edit": "editor",
    "book:render": "editor",
    "book:download": "editor",
    "book:share": "owner",
    "book:delete": "owner",
    "book:transfer_ownership": "owner",
    "workspace:view": "viewer",
    "workspace:comment": "commenter",
    "workspace:edit": "editor",
    "workspace:manage_members": "owner",
    "workspace:manage_settings": "owner",
    "workspace:share": "owner",
    "workspace:delete": "owner",
    "collection:view": "viewer",
    "collection:edit": "editor",
    "collection:manage_collections": "editor",
    # bare-verb fallbacks (used when the action is not namespace-qualified)
    "view": "viewer",
    "comment": "commenter",
    "edit": "editor",
    "render": "editor",
    "download": "editor",
    "manage_collections": "editor",
    "share": "owner",
    "manage_members": "owner",
    "manage_settings": "owner",
    "transfer_ownership": "owner",
    "delete": "owner",
}


# --------------------------------------------------------------------------- #
# Default ABAC rules — the hard-coded invariants made explicit
# --------------------------------------------------------------------------- #

#: An ``admin`` (the auth wildcard role) crosses every boundary. This reproduces
#: ``Principal.can_access_tenant``'s admin bypass and the ``*`` permission.
ADMIN_OVERRIDE = AbacRule(
    name="admin-override",
    actions=frozenset({"*"}),
    condition=Attr(path="subject.is_admin", op="eq", value=True),
    effect=AbacEffect.ALLOW,
    description="admin principals bypass resource scoping",
)

#: The personal owner of a book can never be locked out — exactly the
#: ``_book_personal_owner_role`` invariant in :mod:`app.workspaces.authz`.
PERSONAL_OWNER_OVERRIDE = AbacRule(
    name="personal-owner",
    actions=frozenset({"book:*"}),
    condition=is_owner(),  # subject.id == resource.owner
    effect=AbacEffect.ALLOW,
    description="the personal book owner always has full access",
)

#: Tenant isolation: deny when the subject is tenant-scoped and the resource
#: belongs to a *different* tenant. Reproduces ``can_access_tenant``'s deny.
#: (Admins are exempted by ADMIN_OVERRIDE running first under first-applicable.)
TENANT_ISOLATION = AbacRule(
    name="tenant-isolation",
    actions=frozenset({"*"}),
    condition=AllOf(
        [
            Attr(path="subject.tenant", op="ne", value=None),
            Attr(path="resource.tenant", op="ne", value=None),
            ~same_tenant(),
        ]
    ),
    effect=AbacEffect.DENY,
    description="a tenant-scoped subject cannot touch another tenant's resource",
)

#: The default ABAC rule list, in precedence order (first-applicable):
#: admin override → personal owner → tenant isolation deny.
DEFAULT_ABAC_RULES = (ADMIN_OVERRIDE, PERSONAL_OWNER_OVERRIDE, TENANT_ISOLATION)


# Re-export the auth role catalogue builder for one-call wiring.
def auth_role_catalogue() -> RoleCatalogue:
    """The legacy auth role catalogue, as a plane :class:`RoleCatalogue`."""
    return RoleCatalogue.from_auth_catalogue()


__all__ = [
    "ACTION_RELATION",
    "ADMIN_OVERRIDE",
    "BOOK_NAMESPACE",
    "COLLECTION_NAMESPACE",
    "DEFAULT_ABAC_RULES",
    "KINORA_NAMESPACES",
    "PERSONAL_OWNER_OVERRIDE",
    "TENANT_ISOLATION",
    "WORKSPACE_NAMESPACE",
    "auth_role_catalogue",
]
