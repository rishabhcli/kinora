"""feature flags & experimentation platform tables

Revision ID: f7a3b2c19e44
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28

Adds the four tables behind ``app.flags`` (the feature-flag + A/B experimentation
platform):

* ``feature_flags`` — durable flag registry; the evaluable definition is JSONB.
* ``flag_experiments`` — durable experiment definitions (JSONB).
* ``flag_exposures`` — idempotent exposure log (UNIQUE on ``dedup_key``).
* ``flag_audit`` — append-only change log with before/after + computed diff.

Purely additive: no existing table is touched, so this migration is safe to apply
on top of the current head and trivially reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f7a3b2c19e44"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feature_flags",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_feature_flags"),
        sa.UniqueConstraint("key", name="uq_feature_flags_key"),
    )
    op.create_index(
        "ix_feature_flags_enabled_archived", "feature_flags", ["enabled", "archived"], unique=False
    )

    op.create_table(
        "flag_experiments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_flag_experiments"),
        sa.UniqueConstraint("key", name="uq_flag_experiments_key"),
    )
    op.create_index("ix_flag_experiments_status", "flag_experiments", ["status"], unique=False)

    op.create_table(
        "flag_exposures",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("experiment_key", sa.String(length=128), nullable=False),
        sa.Column("experiment_version", sa.Integer(), nullable=False),
        sa.Column("variant_key", sa.String(length=128), nullable=False),
        sa.Column("unit_key", sa.String(length=256), nullable=False),
        sa.Column("dedup_key", sa.String(length=512), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_flag_exposures"),
        sa.UniqueConstraint("dedup_key", name="uq_flag_exposures_dedup_key"),
    )
    op.create_index(
        "ix_flag_exposures_experiment_key", "flag_exposures", ["experiment_key"], unique=False
    )
    op.create_index(
        "ix_flag_exposures_experiment_variant",
        "flag_exposures",
        ["experiment_key", "variant_key"],
        unique=False,
    )

    op.create_table(
        "flag_audit",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("subject_kind", sa.String(length=32), nullable=False),
        sa.Column("subject_key", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=256), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("changes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_flag_audit"),
    )
    op.create_index(
        "ix_flag_audit_subject",
        "flag_audit",
        ["subject_kind", "subject_key", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_flag_audit_subject", table_name="flag_audit")
    op.drop_table("flag_audit")
    op.drop_index("ix_flag_exposures_experiment_variant", table_name="flag_exposures")
    op.drop_index("ix_flag_exposures_experiment_key", table_name="flag_exposures")
    op.drop_table("flag_exposures")
    op.drop_index("ix_flag_experiments_status", table_name="flag_experiments")
    op.drop_table("flag_experiments")
    op.drop_index("ix_feature_flags_enabled_archived", table_name="feature_flags")
    op.drop_table("feature_flags")
