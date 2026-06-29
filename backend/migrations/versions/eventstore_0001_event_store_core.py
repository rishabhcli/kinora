"""event store core: es_events/snapshots/outbox/inbox/sequence/checkpoints (event sourcing facet A)

Revision ID: eventstore_0001
Revises: a1b2c3d4e5f6
Create Date: 2026-06-29

The append-only **event store** (kinora.md §6 shared blackboard, §8.5 forgetting-
as-scoping, §12.1 idempotent publish + DLQ). Six additive, ``es_``-prefixed
tables; touches no existing table:

* ``es_events`` — the log. ``global_position`` (BIGINT, gap-free, allocated from
  ``es_sequence``) is the PK + store-wide order; unique ``(stream_id, version)``
  is the per-stream order and the hard optimistic-concurrency backstop; unique
  ``event_id`` is the dedup/idempotency key. ``payload`` + ``event_metadata`` are
  JSONB (the metadata envelope carries correlation/causation/actor/headers).
* ``es_snapshots`` — aggregate snapshots, PK ``(stream_id, snapshot_type)``.
* ``es_outbox`` — the transactional OUTBOX; ``(status, available_at)`` index for
  the relay's "due, pending, FOR UPDATE SKIP LOCKED" claim; unique ``event_id``.
* ``es_inbox`` — the idempotent INBOX, PK ``(consumer, message_id)``.
* ``es_sequence`` — the single gap-free global-position counter row.
* ``es_checkpoints`` — durable projection positions for catch-up subscriptions
  (PK ``subscription``); the read side advances this once per processed event.

Purely additive + reversible. Chains the shared trunk head ``a1b2c3d4e5f6``
(every sibling subsystem migration branches off it; the eventual squash-merge
linearises the fan-out).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "eventstore_0001"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- es_events ---------------------------------------------------------- #
    op.create_table(
        "es_events",
        sa.Column("global_position", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("stream_id", sa.String(length=255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=255), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "event_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("global_position", name="pk_es_events"),
        sa.UniqueConstraint("stream_id", "version", name="uq_es_events_stream_id_version"),
        sa.UniqueConstraint("event_id", name="uq_es_events_event_id"),
    )
    op.create_index("ix_es_events_correlation", "es_events", ["correlation_id"])
    op.create_index("ix_es_events_event_type", "es_events", ["event_type"])

    # -- es_snapshots ------------------------------------------------------- #
    op.create_table(
        "es_snapshots",
        sa.Column("stream_id", sa.String(length=255), nullable=False),
        sa.Column("snapshot_type", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("stream_id", "snapshot_type", name="pk_es_snapshots"),
    )

    # -- es_outbox ---------------------------------------------------------- #
    op.create_table(
        "es_outbox",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("global_position", sa.BigInteger(), nullable=False),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "available_at",
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
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_es_outbox"),
        sa.UniqueConstraint("event_id", name="uq_es_outbox_event_id"),
    )
    op.create_index("ix_es_outbox_claim", "es_outbox", ["status", "available_at"])

    # -- es_inbox ----------------------------------------------------------- #
    op.create_table(
        "es_inbox",
        sa.Column("consumer", sa.String(length=255), nullable=False),
        sa.Column("message_id", sa.String(length=255), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("consumer", "message_id", name="pk_es_inbox"),
    )

    # -- es_sequence -------------------------------------------------------- #
    op.create_table(
        "es_sequence",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("name", name="pk_es_sequence"),
    )

    # -- es_checkpoints ----------------------------------------------------- #
    op.create_table(
        "es_checkpoints",
        sa.Column("subscription", sa.String(length=255), nullable=False),
        sa.Column("position", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("events_processed", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("subscription", name="pk_es_checkpoints"),
    )


def downgrade() -> None:
    op.drop_table("es_checkpoints")
    op.drop_table("es_sequence")
    op.drop_table("es_inbox")
    op.drop_index("ix_es_outbox_claim", table_name="es_outbox")
    op.drop_table("es_outbox")
    op.drop_table("es_snapshots")
    op.drop_index("ix_es_events_event_type", table_name="es_events")
    op.drop_index("ix_es_events_correlation", table_name="es_events")
    op.drop_table("es_events")
