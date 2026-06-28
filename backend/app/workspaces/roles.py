"""Roles, actions, and the capability lattice for the workspaces subsystem.

This module is the **vocabulary** of the authorization model and is deliberately
free of any I/O or ORM dependency so the policy engine (:mod:`app.workspaces.policy`)
that consumes it stays a pure, exhaustively unit-testable decision function.

The role model is a small **totally-ordered lattice**::

    OWNER  >  EDITOR  >  COMMENTER  >  VIEWER

Each role grants a *set* of :class:`Action` capabilities; a higher role is a
strict superset of the capabilities of every lower role (so the lattice order
and the capability subset order agree — verified by a test). The most-permissive
applicable grant wins when a principal holds several (e.g. a direct resource
share *and* a workspace-membership-derived role).

Sharing model (kinora.md §5): the shelf is per-user today (``books.user_id``).
A workspace makes a shelf *shareable* by attaching books/collections to it and
granting roles to its members; a resource can also be shared directly with an
individual user. Roles map to the concrete product verbs — read the book in the
two-pane workspace (``view``), drop a Director region-comment (``comment``), edit
the canon / regen a shot (``edit``), spend video-seconds (``render``), and the
administrative verbs (membership, sharing, settings, transfer, delete).
"""

from __future__ import annotations

import enum
from typing import Final

from sqlalchemy import Enum as SAEnum

# --------------------------------------------------------------------------- #
# The role lattice
# --------------------------------------------------------------------------- #


class Role(enum.StrEnum):
    """A collaboration role on a workspace or a shared resource.

    Ordered most-privileged → least: every higher role's capability set is a
    superset of every lower one. Use :func:`role_rank` / the comparison helpers
    rather than comparing the string values directly.
    """

    OWNER = "owner"
    EDITOR = "editor"
    COMMENTER = "commenter"
    VIEWER = "viewer"


#: Rank of each role in the lattice — higher number == more privilege. Used to
#: pick the most-permissive grant when a principal holds several.
_ROLE_RANK: Final[dict[Role, int]] = {
    Role.VIEWER: 0,
    Role.COMMENTER: 1,
    Role.EDITOR: 2,
    Role.OWNER: 3,
}


def role_rank(role: Role) -> int:
    """Return the lattice rank of ``role`` (higher == more privileged)."""
    return _ROLE_RANK[role]


def max_role(*roles: Role | None) -> Role | None:
    """Return the most-permissive of ``roles`` (ignoring ``None``), or ``None``.

    This is how the policy engine collapses several applicable grants — a direct
    share, a workspace membership, the personal owner bit — into one effective
    role: the strongest one wins.
    """
    present = [r for r in roles if r is not None]
    if not present:
        return None
    return max(present, key=role_rank)


def role_at_least(role: Role | None, floor: Role) -> bool:
    """True when ``role`` is present and at least as privileged as ``floor``."""
    return role is not None and role_rank(role) >= role_rank(floor)


# --------------------------------------------------------------------------- #
# Actions (the verbs the engine arbitrates)
# --------------------------------------------------------------------------- #


class Action(enum.StrEnum):
    """A capability a principal may or may not hold on a resource.

    Split into *content* verbs (what a reader/director does inside a book's
    workspace) and *administrative* verbs (managing the collaboration itself).
    """

    # -- content / product verbs (kinora.md §5) ----------------------------- #
    VIEW = "view"  # open the two-pane workspace, watch/read
    COMMENT = "comment"  # drop a Director region-comment (§5.4)
    EDIT = "edit"  # edit the canon, regen a shot, change settings on a book
    RENDER = "render"  # spend video-seconds (promote a render)
    DOWNLOAD = "download"  # export a stitched film / the source

    # -- collaboration / administrative verbs ------------------------------- #
    SHARE = "share"  # grant/revoke another principal's access
    MANAGE_MEMBERS = "manage_members"  # invite/remove/role-change members
    MANAGE_SETTINGS = "manage_settings"  # workspace settings + quotas
    MANAGE_COLLECTIONS = "manage_collections"  # create/edit collections
    TRANSFER_OWNERSHIP = "transfer_ownership"  # hand the resource to someone else
    DELETE = "delete"  # destroy the resource / workspace
    VIEW_ACTIVITY = "view_activity"  # read the activity feed


#: Capabilities granted by each role. Built incrementally so the superset
#: invariant (a higher role ⊇ a lower role) is structural, not hand-maintained.
_VIEWER_ACTIONS: Final[frozenset[Action]] = frozenset({Action.VIEW})
_COMMENTER_ACTIONS: Final[frozenset[Action]] = _VIEWER_ACTIONS | {
    Action.COMMENT,
    Action.VIEW_ACTIVITY,
}
_EDITOR_ACTIONS: Final[frozenset[Action]] = _COMMENTER_ACTIONS | {
    Action.EDIT,
    Action.RENDER,
    Action.DOWNLOAD,
    Action.MANAGE_COLLECTIONS,
}
_OWNER_ACTIONS: Final[frozenset[Action]] = _EDITOR_ACTIONS | {
    Action.SHARE,
    Action.MANAGE_MEMBERS,
    Action.MANAGE_SETTINGS,
    Action.TRANSFER_OWNERSHIP,
    Action.DELETE,
}

ROLE_CAPABILITIES: Final[dict[Role, frozenset[Action]]] = {
    Role.VIEWER: _VIEWER_ACTIONS,
    Role.COMMENTER: _COMMENTER_ACTIONS,
    Role.EDITOR: _EDITOR_ACTIONS,
    Role.OWNER: _OWNER_ACTIONS,
}


def capabilities_for(role: Role) -> frozenset[Action]:
    """Return the set of actions ``role`` is permitted to take."""
    return ROLE_CAPABILITIES[role]


def role_allows(role: Role | None, action: Action) -> bool:
    """True when ``role`` is present and grants ``action`` (the core check)."""
    return role is not None and action in ROLE_CAPABILITIES[role]


def minimum_role_for(action: Action) -> Role | None:
    """The least-privileged role that can perform ``action`` (or ``None``).

    Handy for UIs that want to render a "you need at least Editor to …" hint.
    """
    for role in (Role.VIEWER, Role.COMMENTER, Role.EDITOR, Role.OWNER):
        if action in ROLE_CAPABILITIES[role]:
            return role
    return None


# --------------------------------------------------------------------------- #
# Lifecycle / classification enums
# --------------------------------------------------------------------------- #


class MemberStatus(enum.StrEnum):
    """Lifecycle of a (workspace, user) membership edge."""

    ACTIVE = "active"
    INVITED = "invited"  # placeholder edge created by an invitation
    SUSPENDED = "suspended"  # seat retained, access frozen
    REMOVED = "removed"  # soft-removed; row kept for the audit trail


class InvitationStatus(enum.StrEnum):
    """Lifecycle of an email-token workspace invitation."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


class ResourceType(enum.StrEnum):
    """The polymorphic resource kinds a share/grant can target."""

    BOOK = "book"
    COLLECTION = "collection"
    WORKSPACE = "workspace"


class TransferStatus(enum.StrEnum):
    """Lifecycle of an ownership-transfer request."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CANCELLED = "cancelled"


class OrgPlan(enum.StrEnum):
    """Coarse organization plan tier (drives default seat + quota caps)."""

    FREE = "free"
    TEAM = "team"
    ENTERPRISE = "enterprise"


def str_enum(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    """VARCHAR+CHECK column type storing member *values* (matches db.models.enums).

    Re-declared locally (rather than imported) so the workspaces package is a
    self-contained additive unit that does not reach into the shared enums
    module other agents may be editing in parallel.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [member.value for member in e],
    )


__all__ = [
    "ROLE_CAPABILITIES",
    "Action",
    "InvitationStatus",
    "MemberStatus",
    "OrgPlan",
    "ResourceType",
    "Role",
    "TransferStatus",
    "capabilities_for",
    "max_role",
    "minimum_role_for",
    "role_allows",
    "role_at_least",
    "role_rank",
    "str_enum",
]
