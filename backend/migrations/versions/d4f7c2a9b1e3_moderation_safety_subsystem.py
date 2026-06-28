"""Content moderation & safety subsystem (§9/§10)

Revision ID: d4f7c2a9b1e3
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28

Additive migration for the content-moderation subsystem (``app.moderation``). It
creates five new tables and touches no existing one:

* ``moderation_events`` — one screening outcome (verdict for one piece of content
  at one surface), written by the gate on every call.
* ``moderation_audit`` — the append-only, hash-chained, tamper-evident moderation
  audit log (per-tenant ``seq``), mirroring the canon audit log's discipline (§8).
* ``moderation_review_items`` — the human-review queue carrying the
  takedown/appeal state machine.
* ``moderation_tenant_policies`` — the persisted, configurable per-tenant policy.
* ``moderation_violation_counters`` — the per-actor rolling violation tally for
  repeat-offender escalation.

Everything else (the classifier seam, the policy engine, the gates) is pure code;
this migration only lays the durable tables those services persist to.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4f7c2a9b1e3"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SURFACE_VALUES = (
    "ingest_text",
    "ingest_page",
    "keyframe",
    "clip",
    "comment",
    "narration",
)
_DISPOSITION_VALUES = ("allow", "flag", "block")
_REVIEW_STATE_VALUES = (
    "pending",
    "under_review",
    "approved",
    "rejected",
    "takedown",
    "appealed",
    "appeal_granted",
    "appeal_denied",
    "escalated",
)


def _surface_enum(name: str) -> sa.Enum:
    return sa.Enum(*_SURFACE_VALUES, name=name, native_enum=False)


def _disposition_enum(name: str) -> sa.Enum:
    return sa.Enum(*_DISPOSITION_VALUES, name=name, native_enum=False)


def upgrade() -> None:
    # --- moderation_events --------------------------------------------------- #
    op.create_table(
        "moderation_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("surface", _surface_enum("moderation_event_surface"), nullable=False),
        sa.Column("decision", _disposition_enum("moderation_event_decision"), nullable=False),
        sa.Column("severity", sa.Integer(), nullable=False),
        sa.Column("classifier", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("degraded", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("book_id", sa.String(length=64), nullable=True),
        sa.Column("shot_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("categories", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("labels", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
            name=op.f("fk_moderation_events_book_id_books"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_moderation_events_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_moderation_events")),
    )
    op.create_index(
        op.f("ix_moderation_events_book_id"), "moderation_events", ["book_id"], unique=False
    )
    op.create_index(
        op.f("ix_moderation_events_user_id"), "moderation_events", ["user_id"], unique=False
    )
    op.create_index(
        "ix_moderation_events_tenant",
        "moderation_events",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_moderation_events_book", "moderation_events", ["book_id"], unique=False
    )
    op.create_index(
        "ix_moderation_events_actor",
        "moderation_events",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_moderation_events_decision",
        "moderation_events",
        ["tenant_id", "decision"],
        unique=False,
    )

    # --- moderation_audit ---------------------------------------------------- #
    op.create_table(
        "moderation_audit",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("prev_hash", sa.String(length=64), nullable=True),
        sa.Column("entry_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_moderation_audit")),
        sa.UniqueConstraint("tenant_id", "seq", name="uq_moderation_audit_tenant_id_seq"),
    )
    op.create_index(
        "ix_moderation_audit_tenant_seq",
        "moderation_audit",
        ["tenant_id", "seq"],
        unique=False,
    )
    op.create_index(
        "ix_moderation_audit_target",
        "moderation_audit",
        ["tenant_id", "target_id"],
        unique=False,
    )

    # --- moderation_review_items --------------------------------------------- #
    op.create_table(
        "moderation_review_items",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=True),
        sa.Column("surface", _surface_enum("moderation_review_surface"), nullable=False),
        sa.Column(
            "state",
            sa.Enum(*_REVIEW_STATE_VALUES, name="moderation_review_state", native_enum=False),
            nullable=False,
        ),
        sa.Column("decision", _disposition_enum("moderation_review_decision"), nullable=False),
        sa.Column("severity", sa.Integer(), nullable=False),
        sa.Column("categories", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("book_id", sa.String(length=64), nullable=True),
        sa.Column("shot_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("assignee_id", sa.String(length=128), nullable=True),
        sa.Column("resolver_id", sa.String(length=128), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("state_history", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            ["book_id"],
            ["books.id"],
            name=op.f("fk_moderation_review_items_book_id_books"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["moderation_events.id"],
            name=op.f("fk_moderation_review_items_event_id_moderation_events"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_moderation_review_items_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_moderation_review_items")),
    )
    op.create_index(
        "ix_moderation_review_items_queue",
        "moderation_review_items",
        ["tenant_id", "state", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_moderation_review_items_actor",
        "moderation_review_items",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_moderation_review_items_book",
        "moderation_review_items",
        ["book_id"],
        unique=False,
    )
    op.create_index(
        "ix_moderation_review_items_assignee",
        "moderation_review_items",
        ["assignee_id", "state"],
        unique=False,
    )

    # --- moderation_tenant_policies ------------------------------------------ #
    op.create_table(
        "moderation_tenant_policies",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("strictness", sa.Float(), nullable=False),
        sa.Column("fail_closed_on_degraded", sa.Boolean(), nullable=False),
        sa.Column("serve_flagged", sa.Boolean(), nullable=False),
        sa.Column("policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_moderation_tenant_policies")),
        sa.UniqueConstraint(
            "tenant_id", name="uq_moderation_tenant_policies_tenant_id"
        ),
    )

    # --- moderation_violation_counters --------------------------------------- #
    op.create_table(
        "moderation_violation_counters",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("window_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_violation_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tier", sa.Integer(), nullable=False),
        sa.Column("suspended_until", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_moderation_violation_counters")),
        sa.UniqueConstraint(
            "tenant_id",
            "actor_id",
            name="uq_moderation_violation_counters_tenant_id_actor_id",
        ),
    )
    op.create_index(
        "ix_moderation_violation_counters_tier",
        "moderation_violation_counters",
        ["tenant_id", "tier"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_moderation_violation_counters_tier",
        table_name="moderation_violation_counters",
    )
    op.drop_table("moderation_violation_counters")

    op.drop_table("moderation_tenant_policies")

    op.drop_index(
        "ix_moderation_review_items_assignee", table_name="moderation_review_items"
    )
    op.drop_index("ix_moderation_review_items_book", table_name="moderation_review_items")
    op.drop_index("ix_moderation_review_items_actor", table_name="moderation_review_items")
    op.drop_index("ix_moderation_review_items_queue", table_name="moderation_review_items")
    op.drop_table("moderation_review_items")

    op.drop_index("ix_moderation_audit_target", table_name="moderation_audit")
    op.drop_index("ix_moderation_audit_tenant_seq", table_name="moderation_audit")
    op.drop_table("moderation_audit")

    op.drop_index("ix_moderation_events_decision", table_name="moderation_events")
    op.drop_index("ix_moderation_events_actor", table_name="moderation_events")
    op.drop_index("ix_moderation_events_book", table_name="moderation_events")
    op.drop_index("ix_moderation_events_tenant", table_name="moderation_events")
    op.drop_index(op.f("ix_moderation_events_user_id"), table_name="moderation_events")
    op.drop_index(op.f("ix_moderation_events_book_id"), table_name="moderation_events")
    op.drop_table("moderation_events")
