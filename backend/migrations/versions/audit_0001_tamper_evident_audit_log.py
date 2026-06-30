"""Tamper-evident audit log + provenance (app.audit)

Revision ID: audit_0001
Revises: ace1a7010c30
Create Date: 2026-06-30

Additive migration for the audit / provenance subsystem (``app.audit``). Creates
two new append-only tables and touches no existing one:

* ``audit_log_entries`` — one immutable, hash-chained audit record per
  consequential action (canon mutation, arbitration decision, render
  accept/degrade, budget spend, auth/lockout, config/flag change). The
  application assigns ``seq`` (unique) so the SHA-256 hash chain is deterministic
  and independently re-derivable; ``entry_hash`` is unique too. ``actor_id`` /
  ``target_id`` are opaque strings with no FK so the proof trail survives deletion
  of whatever they reference.
* ``audit_checkpoints`` — one sealed Merkle checkpoint over a contiguous segment
  of entries (a compact, publishable tamper-evidence commitment).

Enum columns are portable VARCHAR + named CHECK (``native_enum=False``), matching
the rest of the schema, so this migration owns no Postgres ENUM object to drop.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "audit_0001"
down_revision: str | None = "ace1a7010c30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CATEGORY = sa.Enum(
    "canon",
    "arbitration",
    "render",
    "budget",
    "auth",
    "config",
    "flag",
    "scheduler",
    "ingest",
    "moderation",
    "system",
    name="audit_category",
    native_enum=False,
    create_constraint=True,
)
_ACTION = sa.Enum(
    "canon.created",
    "canon.updated",
    "canon.deleted",
    "canon.branched",
    "canon.merged",
    "arbitration.opened",
    "arbitration.resolved",
    "arbitration.overridden",
    "render.planned",
    "render.accepted",
    "render.degraded",
    "render.rejected",
    "render.regenerated",
    "budget.reserved",
    "budget.spent",
    "budget.released",
    "budget.exhausted",
    "auth.login",
    "auth.logout",
    "auth.login_failed",
    "auth.locked_out",
    "auth.token_revoked",
    "auth.password_changed",
    "config.changed",
    "flag.enabled",
    "flag.disabled",
    "flag.updated",
    "scheduler.promoted",
    "scheduler.evicted",
    "ingest.started",
    "ingest.completed",
    "ingest.failed",
    "moderation.flagged",
    "moderation.cleared",
    "audit.segment_sealed",
    "audit.retention_pruned",
    "other",
    name="audit_action",
    native_enum=False,
    create_constraint=True,
)
_SEVERITY = sa.Enum(
    "info",
    "notice",
    "warning",
    "critical",
    name="audit_severity",
    native_enum=False,
    create_constraint=True,
)
_ACTOR_KIND = sa.Enum(
    "agent",
    "user",
    "system",
    name="audit_actor_kind",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    op.create_table(
        "audit_log_entries",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("category", _CATEGORY, nullable=False),
        sa.Column("action", _ACTION, nullable=False),
        sa.Column("severity", _SEVERITY, nullable=False),
        sa.Column("actor_kind", _ACTOR_KIND, nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=True),
        sa.Column("target_id", sa.String(length=128), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("entry_hash", sa.String(length=64), nullable=False),
        sa.Column("sealed", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log_entries")),
        sa.UniqueConstraint("seq", name="uq_audit_log_entries_seq"),
        sa.UniqueConstraint("entry_hash", name="uq_audit_log_entries_entry_hash"),
    )
    op.create_index(
        "ix_audit_log_entries_actor", "audit_log_entries", ["actor_id", "seq"], unique=False
    )
    op.create_index(
        "ix_audit_log_entries_target",
        "audit_log_entries",
        ["target_type", "target_id", "seq"],
        unique=False,
    )
    op.create_index(
        "ix_audit_log_entries_correlation",
        "audit_log_entries",
        ["correlation_id", "seq"],
        unique=False,
    )
    op.create_index(
        "ix_audit_log_entries_category_action",
        "audit_log_entries",
        ["category", "action", "seq"],
        unique=False,
    )
    op.create_index(
        "ix_audit_log_entries_occurred_at", "audit_log_entries", ["occurred_at"], unique=False
    )

    op.create_table(
        "audit_checkpoints",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("from_seq", sa.BigInteger(), nullable=False),
        sa.Column("to_seq", sa.BigInteger(), nullable=False),
        sa.Column("merkle_root", sa.String(length=64), nullable=False),
        sa.Column("prev_checkpoint_hash", sa.String(length=64), nullable=False),
        sa.Column("checkpoint_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_checkpoints")),
        sa.UniqueConstraint("seq", name="uq_audit_checkpoints_seq"),
    )
    op.create_index(
        "ix_audit_checkpoints_range", "audit_checkpoints", ["from_seq", "to_seq"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_audit_checkpoints_range", table_name="audit_checkpoints")
    op.drop_table("audit_checkpoints")
    op.drop_index("ix_audit_log_entries_occurred_at", table_name="audit_log_entries")
    op.drop_index("ix_audit_log_entries_category_action", table_name="audit_log_entries")
    op.drop_index("ix_audit_log_entries_correlation", table_name="audit_log_entries")
    op.drop_index("ix_audit_log_entries_target", table_name="audit_log_entries")
    op.drop_index("ix_audit_log_entries_actor", table_name="audit_log_entries")
    op.drop_table("audit_log_entries")
