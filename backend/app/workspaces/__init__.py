"""Workspaces & teams — multi-user collaboration-ownership for Kinora (§5).

A subsystem layered on the existing single-user identity (``users``) and durable
per-book ownership (``books.user_id``). It adds organizations, workspaces (shared
shelves), membership + email-token invitations, role-based sharing of books and
collections, transfer-of-ownership, per-workspace settings + quotas, seat
management, and an activity feed — all behind one clean ``can(user, action,
resource)`` authorization API (:class:`app.workspaces.authz.AuthorizationService`)
that any other domain can call.

See ``DESIGN.md`` for the roadmap and the data model.
"""

from __future__ import annotations

from app.workspaces.policy import Decision, GrantSet, allowed_actions, decide
from app.workspaces.roles import (
    Action,
    InvitationStatus,
    MemberStatus,
    OrgPlan,
    ResourceType,
    Role,
    TransferStatus,
    capabilities_for,
    max_role,
    role_allows,
    role_at_least,
)

__all__ = [
    "Action",
    "Decision",
    "GrantSet",
    "InvitationStatus",
    "MemberStatus",
    "OrgPlan",
    "ResourceType",
    "Role",
    "TransferStatus",
    "allowed_actions",
    "capabilities_for",
    "decide",
    "max_role",
    "role_allows",
    "role_at_least",
]
