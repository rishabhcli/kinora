"""ORM models for the workspaces / teams / collaboration-ownership subsystem.

These tables layer **on top of** the existing single-user identity (``users``)
and the durable per-book owner (``books.user_id``, kinora.md §5.1). Nothing here
mutates an existing table; importing this module only *adds* tables to
``Base.metadata`` (registered via ``app/db/models/__init__.py``).

Ownership semantics (fail-closed, additive):

* the personal ``books.user_id`` owner always keeps owner-level access — a
  workspace grant can only *add* collaborators, never demote the owner;
* a workspace makes a shelf shareable: books are attached to a workspace via the
  ``workspace_books`` association, and a member's role on the workspace flows
  down to those books unless a stronger direct share exists;
* user deletes ``SET NULL`` on owner columns (orphan, don't cascade-delete) but
  ``CASCADE`` on the membership/share *edges* (a deleted user's edges are noise).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin, TimestampMixin
from app.workspaces.roles import (
    InvitationStatus,
    MemberStatus,
    OrgPlan,
    ResourceType,
    Role,
    TransferStatus,
    str_enum,
)


class Organization(StrIdMixin, TimestampMixin, Base):
    """A top-level tenant that owns seats, a plan, and one-or-more workspaces."""

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(256), nullable=False)
    #: The user who created the org; OWNER of every workspace under it. SET NULL
    #: on delete so a removed account orphans the org rather than cascading.
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    plan: Mapped[OrgPlan] = mapped_column(
        str_enum(OrgPlan, "org_plan"), default=OrgPlan.FREE, nullable=False
    )
    #: Purchased seats (active members may not exceed this; 0 == unlimited-by-plan).
    seats: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class Workspace(StrIdMixin, TimestampMixin, Base):
    """A shared library inside an organization (kinora.md §5.1 — a shareable shelf)."""

    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("org_id", "slug", name="uq_workspaces_org_slug"),
        Index("ix_workspaces_org_id", "org_id"),
    )

    org_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    #: URL-safe handle, unique within the org.
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Per-workspace settings + quotas (e.g. ``max_books``, ``video_seconds_cap``,
    #: ``default_member_role``). A JSONB bag so quota knobs evolve without a migration.
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class WorkspaceMember(StrIdMixin, TimestampMixin, Base):
    """A (workspace, user) membership edge carrying a role + lifecycle status."""

    __tablename__ = "workspace_members"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_members_workspace_user"),
        Index("ix_workspace_members_user_id", "user_id"),
        Index("ix_workspace_members_workspace_status", "workspace_id", "status"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[Role] = mapped_column(
        str_enum(Role, "workspace_member_role"), default=Role.VIEWER, nullable=False
    )
    status: Mapped[MemberStatus] = mapped_column(
        str_enum(MemberStatus, "workspace_member_status"),
        default=MemberStatus.ACTIVE,
        nullable=False,
    )
    #: The member who added this one (for the activity feed); SET NULL on delete.
    invited_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class WorkspaceInvitation(StrIdMixin, TimestampMixin, Base):
    """An email-token invitation to join a workspace at a role (the accept flow)."""

    __tablename__ = "workspace_invitations"
    __table_args__ = (
        Index("ix_workspace_invitations_workspace", "workspace_id"),
        Index("ix_workspace_invitations_email_status", "email", "status"),
        # The opaque token is the lookup key for the accept endpoint.
        UniqueConstraint("token", name="uq_workspace_invitations_token"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    #: Invitee email (lower-cased). The invitee need not have an account yet.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[Role] = mapped_column(
        str_enum(Role, "workspace_invitation_role"), default=Role.VIEWER, nullable=False
    )
    #: Opaque, signed acceptance token (HMAC; see ``invitations.py``).
    token: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[InvitationStatus] = mapped_column(
        str_enum(InvitationStatus, "workspace_invitation_status"),
        default=InvitationStatus.PENDING,
        nullable=False,
    )
    invited_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The user who ultimately accepted (resolved at accept time).
    accepted_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class WorkspaceBook(StrIdMixin, CreatedAtMixin, Base):
    """Attaches a book to a workspace so members can reach it (the shared shelf).

    Additive to ``books.user_id``: the personal owner keeps their access; this row
    grants the workspace's members access to the book at their workspace role.
    """

    __tablename__ = "workspace_books"
    __table_args__ = (
        UniqueConstraint("workspace_id", "book_id", name="uq_workspace_books_workspace_book"),
        Index("ix_workspace_books_book_id", "book_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), nullable=False
    )
    added_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ResourceShare(StrIdMixin, TimestampMixin, Base):
    """A direct grant of a role on one resource to one user (polymorphic).

    The resource is identified by ``(resource_type, resource_id)`` so a single
    table covers book / collection / workspace shares. A direct share is the
    strongest *non-owner* path and overrides a weaker workspace-derived role.
    """

    __tablename__ = "resource_shares"
    __table_args__ = (
        UniqueConstraint(
            "resource_type",
            "resource_id",
            "user_id",
            name="uq_resource_shares_resource_user",
        ),
        Index("ix_resource_shares_user_id", "user_id"),
        Index("ix_resource_shares_resource", "resource_type", "resource_id"),
    )

    resource_type: Mapped[ResourceType] = mapped_column(
        str_enum(ResourceType, "resource_share_type"), nullable=False
    )
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[Role] = mapped_column(
        str_enum(Role, "resource_share_role"), default=Role.VIEWER, nullable=False
    )
    granted_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Optional expiry; a share past ``expires_at`` is filtered out before the engine.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Collection(StrIdMixin, TimestampMixin, Base):
    """A named bundle of books inside a workspace (a sub-shelf)."""

    __tablename__ = "collections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug", name="uq_collections_workspace_slug"),
        Index("ix_collections_workspace_id", "workspace_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class CollectionItem(StrIdMixin, CreatedAtMixin, Base):
    """Membership of a book in a collection."""

    __tablename__ = "collection_items"
    __table_args__ = (
        UniqueConstraint("collection_id", "book_id", name="uq_collection_items_collection_book"),
        Index("ix_collection_items_book_id", "book_id"),
    )

    collection_id: Mapped[str] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), nullable=False
    )
    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class OwnershipTransfer(StrIdMixin, TimestampMixin, Base):
    """An audited request to transfer ownership of a resource to another user."""

    __tablename__ = "ownership_transfers"
    __table_args__ = (
        Index("ix_ownership_transfers_resource", "resource_type", "resource_id"),
        Index("ix_ownership_transfers_to_user_status", "to_user_id", "status"),
    )

    resource_type: Mapped[ResourceType] = mapped_column(
        str_enum(ResourceType, "ownership_transfer_type"), nullable=False
    )
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    from_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    to_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[TransferStatus] = mapped_column(
        str_enum(TransferStatus, "ownership_transfer_status"),
        default=TransferStatus.PENDING,
        nullable=False,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkspaceActivity(StrIdMixin, CreatedAtMixin, Base):
    """Append-only activity feed: who did what, where, when (kinora.md §5 feed)."""

    __tablename__ = "workspace_activity"
    __table_args__ = (
        Index("ix_workspace_activity_workspace_created", "workspace_id", "created_at"),
        Index("ix_workspace_activity_actor", "actor_user_id"),
    )

    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True
    )
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: A stable verb string (e.g. ``member.invited``, ``book.shared``).
    verb: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The object the verb acted on (``(resource_type, resource_id)`` when relevant).
    resource_type: Mapped[ResourceType | None] = mapped_column(
        str_enum(ResourceType, "workspace_activity_resource_type"), nullable=True
    )
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Free-form structured context for the feed renderer.
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


__all__ = [
    "Collection",
    "CollectionItem",
    "Organization",
    "OwnershipTransfer",
    "ResourceShare",
    "Workspace",
    "WorkspaceActivity",
    "WorkspaceBook",
    "WorkspaceInvitation",
    "WorkspaceMember",
]
