"""The org/workspace/membership domain model + repository protocols + fakes.

This is the *isolation-layer* view of tenancy (distinct from, and additive to,
the collaboration-shelf models in :mod:`app.workspaces`): an **organization**
owns one-or-more **workspaces**; a user holds a **membership** at a role in an
org and/or a workspace. The resolved role for a (user, workspace) pair is the
most-permissive of their org role and their direct workspace role
(:func:`app.tenancy.roles.effective_role`).

Everything here is framework-agnostic:

* immutable value objects (``Organization``, ``Workspace``, ``Membership``);
* thin :class:`Protocol` repositories (``OrgRepo``, ``WorkspaceRepo``,
  ``MembershipRepo``, ``QuotaRepo``) defining the persistence seam; and
* in-memory fakes implementing them, so the whole service + enforcement story is
  exhaustively unit-testable with **no DB and no network**. The production
  SQLAlchemy adapters can implement the same protocols later against the
  additive tables in :mod:`app.tenancy.models`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from app.tenancy.quota import QuotaEnvelope, QuotaResource, Usage
from app.tenancy.roles import Role


def _new_id() -> str:
    """A fresh opaque id (matches ``app.db.base.new_id`` shape: 32-hex)."""
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Organization:
    """A top-level tenant owning seats, a plan, a quota envelope, and config."""

    id: str
    name: str
    owner_user_id: str
    plan: str = "free"
    seats: int = 5
    envelope: QuotaEnvelope = field(default_factory=QuotaEnvelope)
    config_overrides: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Workspace:
    """A shared library inside an org; may tighten the org's quota + config."""

    id: str
    org_id: str
    name: str
    slug: str
    #: An optional per-workspace envelope; ``None`` inherits the org's envelope.
    envelope: QuotaEnvelope | None = None
    config_overrides: Mapping[str, Any] = field(default_factory=dict)
    archived: bool = False


@dataclass(frozen=True, slots=True)
class Membership:
    """A (user, tenant) edge carrying a role. ``workspace_id is None`` == org-level.

    A user can hold an org-level membership (applies across every workspace) and
    a workspace-level membership (applies only there); the effective role is the
    stronger of the two.
    """

    id: str
    user_id: str
    org_id: str
    role: Role
    workspace_id: str | None = None
    status: str = "active"

    @property
    def is_org_level(self) -> bool:
        return self.workspace_id is None


# --------------------------------------------------------------------------- #
# Repository protocols (the persistence seam)
# --------------------------------------------------------------------------- #


class OrgRepo(Protocol):
    """Persistence for organizations."""

    def get(self, org_id: str) -> Organization | None: ...
    def add(self, org: Organization) -> Organization: ...
    def update(self, org: Organization) -> Organization: ...


class WorkspaceRepo(Protocol):
    """Persistence for workspaces."""

    def get(self, workspace_id: str) -> Workspace | None: ...
    def add(self, workspace: Workspace) -> Workspace: ...
    def list_for_org(self, org_id: str) -> list[Workspace]: ...


class MembershipRepo(Protocol):
    """Persistence for (user, tenant) membership edges."""

    def add(self, membership: Membership) -> Membership: ...
    def remove(self, membership_id: str) -> None: ...
    def org_membership(self, user_id: str, org_id: str) -> Membership | None: ...
    def workspace_membership(self, user_id: str, workspace_id: str) -> Membership | None: ...
    def list_for_user(self, user_id: str) -> list[Membership]: ...
    def list_for_workspace(self, workspace_id: str) -> list[Membership]: ...
    def count_active_in_org(self, org_id: str) -> int: ...


class QuotaRepo(Protocol):
    """Persisted per-tenant usage for the current billing period."""

    def usage(self, tenant_key: str) -> Usage: ...
    def record(self, tenant_key: str, resource: QuotaResource, amount: float) -> Usage: ...
    def reset(self, tenant_key: str) -> None: ...


# --------------------------------------------------------------------------- #
# In-memory fakes (test doubles; deterministic, no I/O)
# --------------------------------------------------------------------------- #


class InMemoryOrgRepo:
    """A dict-backed :class:`OrgRepo`."""

    def __init__(self) -> None:
        self._rows: dict[str, Organization] = {}

    def get(self, org_id: str) -> Organization | None:
        return self._rows.get(org_id)

    def add(self, org: Organization) -> Organization:
        if org.id in self._rows:
            raise ValueError(f"org {org.id!r} already exists")
        self._rows[org.id] = org
        return org

    def update(self, org: Organization) -> Organization:
        if org.id not in self._rows:
            raise KeyError(org.id)
        self._rows[org.id] = org
        return org


class InMemoryWorkspaceRepo:
    """A dict-backed :class:`WorkspaceRepo`."""

    def __init__(self) -> None:
        self._rows: dict[str, Workspace] = {}

    def get(self, workspace_id: str) -> Workspace | None:
        return self._rows.get(workspace_id)

    def add(self, workspace: Workspace) -> Workspace:
        if workspace.id in self._rows:
            raise ValueError(f"workspace {workspace.id!r} already exists")
        self._rows[workspace.id] = workspace
        return workspace

    def list_for_org(self, org_id: str) -> list[Workspace]:
        return [w for w in self._rows.values() if w.org_id == org_id]


class InMemoryMembershipRepo:
    """A dict-backed :class:`MembershipRepo`."""

    def __init__(self) -> None:
        self._rows: dict[str, Membership] = {}

    def add(self, membership: Membership) -> Membership:
        self._rows[membership.id] = membership
        return membership

    def remove(self, membership_id: str) -> None:
        self._rows.pop(membership_id, None)

    def org_membership(self, user_id: str, org_id: str) -> Membership | None:
        for m in self._rows.values():
            if (
                m.user_id == user_id
                and m.org_id == org_id
                and m.is_org_level
                and m.status == "active"
            ):
                return m
        return None

    def workspace_membership(self, user_id: str, workspace_id: str) -> Membership | None:
        for m in self._rows.values():
            if (
                m.user_id == user_id
                and m.workspace_id == workspace_id
                and m.status == "active"
            ):
                return m
        return None

    def list_for_user(self, user_id: str) -> list[Membership]:
        return [m for m in self._rows.values() if m.user_id == user_id]

    def list_for_workspace(self, workspace_id: str) -> list[Membership]:
        return [m for m in self._rows.values() if m.workspace_id == workspace_id]

    def count_active_in_org(self, org_id: str) -> int:
        users = {
            m.user_id
            for m in self._rows.values()
            if m.org_id == org_id and m.status == "active"
        }
        return len(users)


class InMemoryQuotaRepo:
    """A dict-backed :class:`QuotaRepo` keyed by tenant key."""

    def __init__(self) -> None:
        self._rows: dict[str, Usage] = {}

    def usage(self, tenant_key: str) -> Usage:
        return self._rows.get(tenant_key, Usage())

    def record(self, tenant_key: str, resource: QuotaResource, amount: float) -> Usage:
        current = self._rows.get(tenant_key, Usage())
        updated = current.with_charge(resource, amount)
        self._rows[tenant_key] = updated
        return updated

    def reset(self, tenant_key: str) -> None:
        self._rows[tenant_key] = Usage()


@dataclass(slots=True)
class InMemoryTenancyStore:
    """Bundle of the four in-memory fakes, with a small seeding convenience."""

    orgs: InMemoryOrgRepo = field(default_factory=InMemoryOrgRepo)
    workspaces: InMemoryWorkspaceRepo = field(default_factory=InMemoryWorkspaceRepo)
    memberships: InMemoryMembershipRepo = field(default_factory=InMemoryMembershipRepo)
    quotas: InMemoryQuotaRepo = field(default_factory=InMemoryQuotaRepo)

    def seed_org(
        self,
        *,
        owner_user_id: str,
        name: str = "Acme",
        envelope: QuotaEnvelope | None = None,
        org_id: str | None = None,
    ) -> Organization:
        """Create an org + an owner membership, returning the org."""
        org = Organization(
            id=org_id or _new_id(),
            name=name,
            owner_user_id=owner_user_id,
            envelope=envelope or QuotaEnvelope(),
        )
        self.orgs.add(org)
        self.memberships.add(
            Membership(
                id=_new_id(),
                user_id=owner_user_id,
                org_id=org.id,
                role=Role.OWNER,
            )
        )
        return org

    def seed_workspace(
        self,
        org: Organization,
        *,
        name: str = "Team",
        slug: str = "team",
        envelope: QuotaEnvelope | None = None,
        workspace_id: str | None = None,
    ) -> Workspace:
        """Create a workspace under ``org``, returning it."""
        ws = Workspace(
            id=workspace_id or _new_id(),
            org_id=org.id,
            name=name,
            slug=slug,
            envelope=envelope,
        )
        self.workspaces.add(ws)
        return ws

    def add_member(
        self,
        *,
        user_id: str,
        org: Organization,
        role: Role,
        workspace: Workspace | None = None,
    ) -> Membership:
        """Add a membership edge (org-level if ``workspace`` is ``None``)."""
        membership = Membership(
            id=_new_id(),
            user_id=user_id,
            org_id=org.id,
            role=role,
            workspace_id=workspace.id if workspace is not None else None,
        )
        return self.memberships.add(membership)


def with_envelope(org: Organization, envelope: QuotaEnvelope) -> Organization:
    """Return a copy of ``org`` with a new quota envelope (immutable update)."""
    return replace(org, envelope=envelope)


__all__ = [
    "InMemoryMembershipRepo",
    "InMemoryOrgRepo",
    "InMemoryQuotaRepo",
    "InMemoryTenancyStore",
    "InMemoryWorkspaceRepo",
    "Membership",
    "MembershipRepo",
    "OrgRepo",
    "Organization",
    "QuotaRepo",
    "Workspace",
    "WorkspaceRepo",
    "with_envelope",
]
