"""workspaces and teams — multi-user collaboration-ownership (kinora.md §5)

Revision ID: f2a9c4d10e7b
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28 17:44:26.618451

Adds the workspaces / teams subsystem (``backend/app/workspaces/``): organizations,
workspaces (shared shelves), membership + email-token invitations, polymorphic
resource shares, workspace↔book attachment, collections, ownership transfers, and
an append-only activity feed.

Purely **additive** — it creates new tables only and touches no existing schema.
The personal book owner (``books.user_id``, ``c8f1a2b3d4e5``) is unchanged; this
subsystem layers shareable access on top of it. User deletes ``SET NULL`` on owner
columns (orphan, don't cascade) and ``CASCADE`` on the membership/share edges.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f2a9c4d10e7b"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("owner_user_id", sa.String(length=64), nullable=True),
        sa.Column(
            "plan",
            sa.Enum("free", "team", "enterprise", name="org_plan", native_enum=False),
            nullable=False,
        ),
        sa.Column("seats", sa.Integer(), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_organizations_owner_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
    )
    op.create_index(
        op.f("ix_organizations_owner_user_id"),
        "organizations",
        ["owner_user_id"],
        unique=False,
    )

    op.create_table(
        "workspaces",
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_workspaces_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
        sa.UniqueConstraint("org_id", "slug", name="uq_workspaces_org_slug"),
    )
    op.create_index("ix_workspaces_org_id", "workspaces", ["org_id"], unique=False)

    op.create_table(
        "workspace_members",
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "owner", "editor", "commenter", "viewer",
                name="workspace_member_role", native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "active", "invited", "suspended", "removed",
                name="workspace_member_status", native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("invited_by_user_id", sa.String(length=64), nullable=True),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"],
            ["users.id"],
            name=op.f("fk_workspace_members_invited_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_workspace_members_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_members_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_members")),
        sa.UniqueConstraint(
            "workspace_id", "user_id", name="uq_workspace_members_workspace_user"
        ),
    )
    op.create_index(
        "ix_workspace_members_user_id", "workspace_members", ["user_id"], unique=False
    )
    op.create_index(
        "ix_workspace_members_workspace_status",
        "workspace_members",
        ["workspace_id", "status"],
        unique=False,
    )

    op.create_table(
        "workspace_invitations",
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "owner", "editor", "commenter", "viewer",
                name="workspace_invitation_role", native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("token", sa.String(length=512), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "accepted", "revoked", "expired",
                name="workspace_invitation_status", native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("invited_by_user_id", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by_user_id", sa.String(length=64), nullable=True),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["accepted_by_user_id"],
            ["users.id"],
            name=op.f("fk_workspace_invitations_accepted_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"],
            ["users.id"],
            name=op.f("fk_workspace_invitations_invited_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_invitations_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_invitations")),
        sa.UniqueConstraint("token", name="uq_workspace_invitations_token"),
    )
    op.create_index(
        "ix_workspace_invitations_email_status",
        "workspace_invitations",
        ["email", "status"],
        unique=False,
    )
    op.create_index(
        "ix_workspace_invitations_workspace",
        "workspace_invitations",
        ["workspace_id"],
        unique=False,
    )

    op.create_table(
        "workspace_books",
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("added_by_user_id", sa.String(length=64), nullable=True),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["added_by_user_id"],
            ["users.id"],
            name=op.f("fk_workspace_books_added_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
            name=op.f("fk_workspace_books_book_id_books"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_books_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_books")),
        sa.UniqueConstraint(
            "workspace_id", "book_id", name="uq_workspace_books_workspace_book"
        ),
    )
    op.create_index(
        "ix_workspace_books_book_id", "workspace_books", ["book_id"], unique=False
    )

    op.create_table(
        "resource_shares",
        sa.Column(
            "resource_type",
            sa.Enum(
                "book", "collection", "workspace",
                name="resource_share_type", native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("resource_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "owner", "editor", "commenter", "viewer",
                name="resource_share_role", native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("granted_by_user_id", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"],
            ["users.id"],
            name=op.f("fk_resource_shares_granted_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_resource_shares_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resource_shares")),
        sa.UniqueConstraint(
            "resource_type", "resource_id", "user_id",
            name="uq_resource_shares_resource_user",
        ),
    )
    op.create_index(
        "ix_resource_shares_resource",
        "resource_shares",
        ["resource_type", "resource_id"],
        unique=False,
    )
    op.create_index(
        "ix_resource_shares_user_id", "resource_shares", ["user_id"], unique=False
    )

    op.create_table(
        "collections",
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=64), nullable=True),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_collections_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_collections_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collections")),
        sa.UniqueConstraint(
            "workspace_id", "slug", name="uq_collections_workspace_slug"
        ),
    )
    op.create_index(
        "ix_collections_workspace_id", "collections", ["workspace_id"], unique=False
    )

    op.create_table(
        "collection_items",
        sa.Column("collection_id", sa.String(length=64), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
            name=op.f("fk_collection_items_book_id_books"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            name=op.f("fk_collection_items_collection_id_collections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collection_items")),
        sa.UniqueConstraint(
            "collection_id", "book_id", name="uq_collection_items_collection_book"
        ),
    )
    op.create_index(
        "ix_collection_items_book_id", "collection_items", ["book_id"], unique=False
    )

    op.create_table(
        "ownership_transfers",
        sa.Column(
            "resource_type",
            sa.Enum(
                "book", "collection", "workspace",
                name="ownership_transfer_type", native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("resource_id", sa.String(length=64), nullable=False),
        sa.Column("from_user_id", sa.String(length=64), nullable=True),
        sa.Column("to_user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "accepted", "declined", "cancelled",
                name="ownership_transfer_status", native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["from_user_id"],
            ["users.id"],
            name=op.f("fk_ownership_transfers_from_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["to_user_id"],
            ["users.id"],
            name=op.f("fk_ownership_transfers_to_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ownership_transfers")),
    )
    op.create_index(
        "ix_ownership_transfers_resource",
        "ownership_transfers",
        ["resource_type", "resource_id"],
        unique=False,
    )
    op.create_index(
        "ix_ownership_transfers_to_user_status",
        "ownership_transfers",
        ["to_user_id", "status"],
        unique=False,
    )

    op.create_table(
        "workspace_activity",
        sa.Column("workspace_id", sa.String(length=64), nullable=True),
        sa.Column("actor_user_id", sa.String(length=64), nullable=True),
        sa.Column("verb", sa.String(length=64), nullable=False),
        sa.Column(
            "resource_type",
            sa.Enum(
                "book", "collection", "workspace",
                name="workspace_activity_resource_type", native_enum=False,
            ),
            nullable=True,
        ),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_workspace_activity_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_activity_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_activity")),
    )
    op.create_index(
        "ix_workspace_activity_actor",
        "workspace_activity",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_workspace_activity_workspace_created",
        "workspace_activity",
        ["workspace_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_activity_workspace_created", table_name="workspace_activity"
    )
    op.drop_index("ix_workspace_activity_actor", table_name="workspace_activity")
    op.drop_table("workspace_activity")
    op.drop_index(
        "ix_ownership_transfers_to_user_status", table_name="ownership_transfers"
    )
    op.drop_index("ix_ownership_transfers_resource", table_name="ownership_transfers")
    op.drop_table("ownership_transfers")
    op.drop_index("ix_collection_items_book_id", table_name="collection_items")
    op.drop_table("collection_items")
    op.drop_index("ix_collections_workspace_id", table_name="collections")
    op.drop_table("collections")
    op.drop_index("ix_resource_shares_user_id", table_name="resource_shares")
    op.drop_index("ix_resource_shares_resource", table_name="resource_shares")
    op.drop_table("resource_shares")
    op.drop_index("ix_workspace_books_book_id", table_name="workspace_books")
    op.drop_table("workspace_books")
    op.drop_index(
        "ix_workspace_invitations_workspace", table_name="workspace_invitations"
    )
    op.drop_index(
        "ix_workspace_invitations_email_status", table_name="workspace_invitations"
    )
    op.drop_table("workspace_invitations")
    op.drop_index(
        "ix_workspace_members_workspace_status", table_name="workspace_members"
    )
    op.drop_index("ix_workspace_members_user_id", table_name="workspace_members")
    op.drop_table("workspace_members")
    op.drop_index("ix_workspaces_org_id", table_name="workspaces")
    op.drop_table("workspaces")
    op.drop_index(op.f("ix_organizations_owner_user_id"), table_name="organizations")
    op.drop_table("organizations")
