"""The pure authorization policy engine.

This is the side-effect-free *decision* half of the workspaces authz model. It
takes a principal, an action, and a fully-resolved set of **grants** (the role a
principal holds through each path that touches a resource) and returns a
:class:`Decision`. It performs no I/O: the DB-backed
:class:`~app.workspaces.authz.AuthorizationService` does the resolution and hands
the result here, which keeps every permission rule exhaustively unit-testable
without infrastructure.

Resolution model — a principal can reach a resource through several *paths*:

* **personal owner** — the durable ``books.user_id`` owner is always treated as
  :data:`~app.workspaces.roles.Role.OWNER` of that book (additive to anything the
  workspace grants; the personal owner can never be locked out of their own book);
* **direct share** — a ``resource_shares`` row granting this user a role on the
  exact resource;
* **workspace membership** — the user's active membership role on the workspace
  that contains the resource (a book attached to a workspace, a collection in it,
  or the workspace itself);
* **org owner** — the organization owner is OWNER of every workspace under the org.

The engine collapses all applicable paths to the **most-permissive** role
(:func:`~app.workspaces.roles.max_role`) and checks whether that role's capability
set contains the action. Suspended/removed memberships and expired shares are
filtered out *before* they reach the engine, so a grant present here is always
live.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.workspaces.roles import (
    Action,
    Role,
    capabilities_for,
    max_role,
    minimum_role_for,
    role_allows,
)


@dataclass(frozen=True, slots=True)
class GrantSet:
    """Every live role a principal holds on one resource, by path.

    Each field is the role obtained via that path (or ``None`` if that path does
    not apply). The engine never re-derives these — it only arbitrates between
    them — so a value present here is already confirmed live (not suspended /
    expired / for a different user).
    """

    personal_owner: Role | None = None
    direct_share: Role | None = None
    workspace_role: Role | None = None
    org_owner: Role | None = None
    #: Free-form extra grants (e.g. a future collection-level grant). Folded in
    #: with the rest when computing the effective role.
    extra: tuple[Role, ...] = field(default_factory=tuple)

    def effective_role(self) -> Role | None:
        """The single strongest role across all paths (or ``None`` if none apply)."""
        return max_role(
            self.personal_owner,
            self.direct_share,
            self.workspace_role,
            self.org_owner,
            *self.extra,
        )

    def is_empty(self) -> bool:
        """True when the principal has no grant on the resource by any path."""
        return self.effective_role() is None


@dataclass(frozen=True, slots=True)
class Decision:
    """The result of a policy evaluation — allow/deny plus an explanation.

    ``allowed`` is the answer ``can(...)`` returns; the rest is for the API's
    typed 403 body and for debugging ("why was I denied?").
    """

    allowed: bool
    action: Action
    effective_role: Role | None
    reason: str

    def __bool__(self) -> bool:  # ``if decide(...):`` reads naturally
        return self.allowed


def decide(action: Action, grants: GrantSet) -> Decision:
    """Decide whether ``grants`` permits ``action`` (the pure core).

    The most-permissive applicable role wins; the action is allowed iff that
    role's capability set contains it.
    """
    role = grants.effective_role()
    if role is None:
        return Decision(
            allowed=False,
            action=action,
            effective_role=None,
            reason="no grant on this resource",
        )
    if role_allows(role, action):
        return Decision(
            allowed=True,
            action=action,
            effective_role=role,
            reason=f"role '{role.value}' permits '{action.value}'",
        )
    needed = minimum_role_for(action)
    needed_label = needed.value if needed is not None else "a higher"
    return Decision(
        allowed=False,
        action=action,
        effective_role=role,
        reason=(
            f"role '{role.value}' lacks '{action.value}'; "
            f"requires at least '{needed_label}'"
        ),
    )


def allowed_actions(grants: GrantSet) -> frozenset[Action]:
    """Every action the principal may take given ``grants`` (for UI capability hints)."""
    role = grants.effective_role()
    if role is None:
        return frozenset()
    return capabilities_for(role)


__all__ = [
    "Decision",
    "GrantSet",
    "allowed_actions",
    "decide",
]
