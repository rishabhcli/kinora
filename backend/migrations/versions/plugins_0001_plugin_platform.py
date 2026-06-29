"""plugin_platform tables (sandboxed extension platform, app.platform.plugins)

Revision ID: plugins_0001
Revises: s1a2b3c4d5e6
Create Date: 2026-06-29

The five tables backing the sandboxed plugin/extension platform
(``backend/app/platform/plugins/``). All are additive and self-contained; the
ORM lives in ``app.platform.plugins.db_models`` and is registered on
``Base.metadata`` via ``app/db/models/__init__.py``.

* ``plugin_registry`` — the marketplace catalog: one row per published
  ``(plugin_id, version)`` artifact. The validated manifest is stored as JSONB
  alongside the plugin source text; ``digest`` (UNIQUE) is the content hash that
  makes a re-publish idempotent and tamper-evident; ``status`` is the review
  state; rating/install counters are mirrored for cheap listing reads.
* ``plugin_installation`` — one row per ``(owner, plugin_id)`` lifecycle state
  (installed/enabled/disabled/upgrading/quarantined/uninstalled) with the
  granted-capability set and the JSONB version ``history`` ledger for rollback.
* ``plugin_review`` — append-only moderation decisions on an artifact.
* ``plugin_rating`` — one rating per ``(plugin_id, user_id)`` (UNIQUE → upsert).
* ``plugin_audit`` — append-only lifecycle/registry change log.

Purely additive + reversible. Chains an existing head (``s1a2b3c4d5e6``); the
final marathon merge reconciles the multiple parallel heads.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "plugins_0001"
down_revision: str | None = "s1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plugin_registry",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("plugin_id", sa.String(length=160), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("publisher", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("max_risk", sa.String(length=16), nullable=False, server_default="low"),
        sa.Column("yanked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("signed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("signature", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("rating_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rating_sum", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("install_count", sa.Integer(), nullable=False, server_default="0"),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plugin_registry")),
        sa.UniqueConstraint("plugin_id", "version", name="uq_plugin_registry_plugin_id_version"),
        sa.UniqueConstraint("digest", name="uq_plugin_registry_digest"),
    )
    op.create_index("ix_plugin_registry_status_risk", "plugin_registry", ["status", "max_risk"])
    op.create_index("ix_plugin_registry_plugin", "plugin_registry", ["plugin_id", "yanked"])

    op.create_table(
        "plugin_installation",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner", sa.String(length=160), nullable=False),
        sa.Column("plugin_id", sa.String(length=160), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="installed"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "granted", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"
        ),
        sa.Column(
            "history", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plugin_installation")),
        sa.UniqueConstraint("owner", "plugin_id", name="uq_plugin_installation_owner_plugin_id"),
    )
    op.create_index("ix_plugin_installation_owner_state", "plugin_installation", ["owner", "state"])

    op.create_table(
        "plugin_review",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("plugin_id", sa.String(length=160), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reviewer", sa.String(length=160), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plugin_review")),
    )
    op.create_index(
        "ix_plugin_review_artifact", "plugin_review", ["plugin_id", "version", "created_at"]
    )

    op.create_table(
        "plugin_rating",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("plugin_id", sa.String(length=160), nullable=False),
        sa.Column("user_id", sa.String(length=160), nullable=False),
        sa.Column("stars", sa.Integer(), nullable=False),
        sa.Column("review", sa.Text(), nullable=False, server_default=""),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plugin_rating")),
        sa.UniqueConstraint("plugin_id", "user_id", name="uq_plugin_rating_plugin_id_user_id"),
    )
    op.create_index("ix_plugin_rating_plugin", "plugin_rating", ["plugin_id"])

    op.create_table(
        "plugin_audit",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("plugin_id", sa.String(length=160), nullable=False),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("actor", sa.String(length=160), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plugin_audit")),
    )
    op.create_index("ix_plugin_audit_subject", "plugin_audit", ["plugin_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_plugin_audit_subject", table_name="plugin_audit")
    op.drop_table("plugin_audit")
    op.drop_index("ix_plugin_rating_plugin", table_name="plugin_rating")
    op.drop_table("plugin_rating")
    op.drop_index("ix_plugin_review_artifact", table_name="plugin_review")
    op.drop_table("plugin_review")
    op.drop_index("ix_plugin_installation_owner_state", table_name="plugin_installation")
    op.drop_table("plugin_installation")
    op.drop_index("ix_plugin_registry_plugin", table_name="plugin_registry")
    op.drop_index("ix_plugin_registry_status_risk", table_name="plugin_registry")
    op.drop_table("plugin_registry")
