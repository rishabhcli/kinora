"""Multi-tenant isolation: tenant_* org/workspace/membership/usage tables

Revision ID: tenancy_0001
Revises: ace1a7010c30
Create Date: 2026-06-30 00:00:00.000000

Additive migration for the multi-tenant isolation layer (``app.tenancy``). It
stacks on the platform re-tier merge head (``ace1a7010c30``) and is CREATE-only —
it touches no existing table. Four tables, under a distinct ``tenant_*`` namespace
so they never collide with the collaboration-shelf tables in ``app.workspaces``:

* ``tenant_orgs`` — a top-level tenant (organization) owning seats, a plan, and a
  per-tenant quota envelope (book count / monthly USD / monthly video-seconds;
  ``0`` == unlimited-by-envelope, still composed with the global ceiling).
* ``tenant_workspaces`` — a workspace nested under an org; a ``NULL`` cap inherits
  the org envelope, a set cap *tightens* it. Carries a denormalised ``tenant_key``
  (``ws:<id>``) so the query guard has one uniform column to filter on.
* ``tenant_memberships`` — a (user, tenant) edge with a role; ``workspace_id`` NULL
  means an org-level membership that applies across every workspace.
* ``tenant_usage`` — the per-tenant consumed-usage ledger for the current billing
  period (the quota counters that ``app.tenancy.quota`` enforces against).

The orchestrator merges this head with the other final-round heads via
``alembic merge heads``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "tenancy_0001"
down_revision: str | None = "ace1a7010c30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_orgs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("owner_user_id", sa.String(length=64), nullable=True),
        sa.Column("plan", sa.String(length=64), nullable=False, server_default="free"),
        sa.Column("seats", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("max_books", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("monthly_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "monthly_video_seconds", sa.Float(), nullable=False, server_default="0"
        ),
        sa.Column(
            "config_overrides",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_tenant_orgs_owner_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_orgs")),
    )
    op.create_index(
        op.f("ix_tenant_orgs_owner_user_id"),
        "tenant_orgs",
        ["owner_user_id"],
        unique=False,
    )

    op.create_table(
        "tenant_workspaces",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_key", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("max_books", sa.Integer(), nullable=True),
        sa.Column("monthly_usd", sa.Float(), nullable=True),
        sa.Column("monthly_video_seconds", sa.Float(), nullable=True),
        sa.Column(
            "config_overrides",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "archived", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["tenant_orgs.id"],
            name=op.f("fk_tenant_workspaces_org_id_tenant_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_workspaces")),
        sa.UniqueConstraint(
            "org_id", "slug", name="uq_tenant_workspaces_org_slug"
        ),
    )
    op.create_index(
        "ix_tenant_workspaces_org_id", "tenant_workspaces", ["org_id"], unique=False
    )
    op.create_index(
        "ix_tenant_workspaces_tenant_key",
        "tenant_workspaces",
        ["tenant_key"],
        unique=False,
    )

    op.create_table(
        "tenant_memberships",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="viewer"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_tenant_memberships_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["tenant_orgs.id"],
            name=op.f("fk_tenant_memberships_org_id_tenant_orgs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["tenant_workspaces.id"],
            name=op.f("fk_tenant_memberships_workspace_id_tenant_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_memberships")),
        sa.UniqueConstraint(
            "user_id",
            "org_id",
            "workspace_id",
            name="uq_tenant_memberships_user_org_workspace",
        ),
    )
    op.create_index(
        "ix_tenant_memberships_user_id", "tenant_memberships", ["user_id"], unique=False
    )
    op.create_index(
        "ix_tenant_memberships_org_id", "tenant_memberships", ["org_id"], unique=False
    )
    op.create_index(
        "ix_tenant_memberships_workspace_id",
        "tenant_memberships",
        ["workspace_id"],
        unique=False,
    )

    op.create_table(
        "tenant_usage",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_key", sa.String(length=80), nullable=False),
        sa.Column("period", sa.String(length=16), nullable=False, server_default="all"),
        sa.Column("books", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("video_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_usage")),
        sa.UniqueConstraint(
            "tenant_key", "period", name="uq_tenant_usage_tenant_period"
        ),
    )
    op.create_index(
        "ix_tenant_usage_tenant_key", "tenant_usage", ["tenant_key"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_tenant_usage_tenant_key", table_name="tenant_usage")
    op.drop_table("tenant_usage")
    op.drop_index(
        "ix_tenant_memberships_workspace_id", table_name="tenant_memberships"
    )
    op.drop_index("ix_tenant_memberships_org_id", table_name="tenant_memberships")
    op.drop_index("ix_tenant_memberships_user_id", table_name="tenant_memberships")
    op.drop_table("tenant_memberships")
    op.drop_index(
        "ix_tenant_workspaces_tenant_key", table_name="tenant_workspaces"
    )
    op.drop_index("ix_tenant_workspaces_org_id", table_name="tenant_workspaces")
    op.drop_table("tenant_workspaces")
    op.drop_index(op.f("ix_tenant_orgs_owner_user_id"), table_name="tenant_orgs")
    op.drop_table("tenant_orgs")
