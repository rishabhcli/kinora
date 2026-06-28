"""Notifications & webhooks platform tables

Revision ID: n1f2a3b4c5d6
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28 12:00:00.000000

Additive migration stacked on the bitemporal-canon head. Adds the five tables of
the notifications platform (kinora.md §5 events + §12 reliability): per-user
preferences, registered webhook endpoints, the idempotent outbox, delivery-status
tracking, and the dead-letter store. It touches no existing table — every table
only references ``users`` via an ``ON DELETE CASCADE`` foreign key.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "n1f2a3b4c5d6"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def upgrade() -> None:
    # -- notification_preferences ------------------------------------------- #
    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("enabled_channels", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("matrix", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("quiet_hours", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("digest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("locale", sa.String(length=16), nullable=False),
        _created_at(),
        _updated_at(),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_preferences_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_preferences")),
        sa.UniqueConstraint("user_id", name="uq_notification_preferences_user_id"),
    )
    op.create_index(
        op.f("ix_notification_preferences_user_id"),
        "notification_preferences",
        ["user_id"],
        unique=False,
    )

    # -- webhook_endpoints --------------------------------------------------- #
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("secret", sa.String(length=128), nullable=False),
        sa.Column("events", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("description", sa.String(length=256), nullable=True),
        _created_at(),
        _updated_at(),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_webhook_endpoints_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_endpoints")),
    )
    op.create_index(
        op.f("ix_webhook_endpoints_user_id"), "webhook_endpoints", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_webhook_endpoints_active"), "webhook_endpoints", ["active"], unique=False
    )

    # -- notification_outbox ------------------------------------------------- #
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _created_at(),
        _updated_at(),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_outbox_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_outbox")),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_notification_outbox_idempotency_key"
        ),
    )
    op.create_index(
        op.f("ix_notification_outbox_idempotency_key"),
        "notification_outbox",
        ["idempotency_key"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_outbox_user_id"), "notification_outbox", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_notification_outbox_status"), "notification_outbox", ["status"], unique=False
    )

    # -- notification_deliveries --------------------------------------------- #
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("notification_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.String(length=256), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_deliveries_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_deliveries")),
    )
    op.create_index(
        op.f("ix_notification_deliveries_notification_id"),
        "notification_deliveries",
        ["notification_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_deliveries_user_id"),
        "notification_deliveries",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_deliveries_status"),
        "notification_deliveries",
        ["status"],
        unique=False,
    )

    # -- notification_inbox -------------------------------------------------- #
    op.create_table(
        "notification_inbox",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("read", sa.Boolean(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_inbox_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_inbox")),
    )
    op.create_index(
        op.f("ix_notification_inbox_user_id"), "notification_inbox", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_notification_inbox_book_id"), "notification_inbox", ["book_id"], unique=False
    )
    op.create_index(
        op.f("ix_notification_inbox_read"), "notification_inbox", ["read"], unique=False
    )

    # -- notification_deadletters -------------------------------------------- #
    op.create_table(
        "notification_deadletters",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("notification_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_deadletters_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_deadletters")),
    )
    op.create_index(
        op.f("ix_notification_deadletters_notification_id"),
        "notification_deadletters",
        ["notification_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_deadletters_user_id"),
        "notification_deadletters",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("notification_deadletters")
    op.drop_table("notification_inbox")
    op.drop_table("notification_deliveries")
    op.drop_table("notification_outbox")
    op.drop_table("webhook_endpoints")
    op.drop_table("notification_preferences")
