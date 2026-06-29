"""Lakehouse feature store: feature_store_* durable tables

Revision ID: featstore_0001
Revises: r3c8a1d7f2b9
Create Date: 2026-06-29 00:00:00.000000

Additive migration for the lakehouse feature store (``app.lakehouse.features``).
It stacks on the recommendations head (the feature store *serves* the
recommendations engine + future ML) and touches no existing table — the three
``feature_store_*`` tables carry no foreign keys into existing tables (an entity
key is an opaque string, since a feature view may be keyed on a non-user/non-book
entity):

* ``feature_store_offline_rows`` — the durable offline feature history (the
  point-in-time join's source). Schemaless JSONB payload so a new feature never
  needs a migration. A unique index on (view, key, event_ts, created_ts) makes the
  append idempotent.
* ``feature_store_view_defs`` — a content-addressed snapshot of every registered
  feature-view definition (name + version + JSON definition).
* ``feature_store_materializations`` — the offline→online materialisation run log
  (view, version, as-of, rows written, coverage) feeding freshness + lineage.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "featstore_0001"
down_revision: str | None = "r3c8a1d7f2b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feature_store_offline_rows",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("view_name", sa.String(length=128), nullable=False),
        sa.Column("entity_key", sa.String(length=512), nullable=False),
        sa.Column("keys", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feature_store_offline_rows")),
    )
    op.create_index(
        "ix_feature_store_offline_view_key_event",
        "feature_store_offline_rows",
        ["view_name", "entity_key", "event_timestamp"],
        unique=False,
    )
    op.create_index(
        "uq_feature_store_offline_identity",
        "feature_store_offline_rows",
        ["view_name", "entity_key", "event_timestamp", "created_timestamp"],
        unique=True,
    )

    op.create_table(
        "feature_store_view_defs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("view_name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feature_store_view_defs")),
    )
    op.create_index(
        "uq_feature_store_view_defs_name_version",
        "feature_store_view_defs",
        ["view_name", "version"],
        unique=True,
    )

    op.create_table(
        "feature_store_materializations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("view_name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rows_written", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("keys_total", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("coverage", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feature_store_materializations")),
    )
    op.create_index(
        "ix_feature_store_materializations_view_asof",
        "feature_store_materializations",
        ["view_name", "as_of"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_feature_store_materializations_view_asof",
        table_name="feature_store_materializations",
    )
    op.drop_table("feature_store_materializations")

    op.drop_index(
        "uq_feature_store_view_defs_name_version", table_name="feature_store_view_defs"
    )
    op.drop_table("feature_store_view_defs")

    op.drop_index(
        "uq_feature_store_offline_identity", table_name="feature_store_offline_rows"
    )
    op.drop_index(
        "ix_feature_store_offline_view_key_event", table_name="feature_store_offline_rows"
    )
    op.drop_table("feature_store_offline_rows")
