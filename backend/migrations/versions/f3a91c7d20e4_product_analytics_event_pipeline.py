"""Product analytics: events, sessions, daily rollup

Revision ID: f3a91c7d20e4
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28 11:30:00.000000

Additive migration for the product-analytics subsystem (``app/analytics/``).
Adds three tables — the scrubbed event log, derived reading sessions, and the
pre-aggregated daily/period rollup — that are deliberately *not* foreign-keyed
to ``books``/``users`` (analytics is historical and must survive deletions;
``anon_user_id`` is a pseudonym, not a user PK). Touches no existing table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f3a91c7d20e4"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analytics_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("anon_user_id", sa.String(length=64), nullable=True),
        sa.Column("book_id", sa.String(length=64), nullable=True),
        sa.Column("session_key", sa.String(length=64), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=True),
        sa.Column("props", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analytics_events")),
        sa.UniqueConstraint("event_id", name="uq_analytics_events_event_id"),
    )
    op.create_index(
        "ix_analytics_events_book_occurred",
        "analytics_events",
        ["book_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_analytics_events_user_occurred",
        "analytics_events",
        ["anon_user_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_analytics_events_name_occurred",
        "analytics_events",
        ["name", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_analytics_events_session", "analytics_events", ["session_key"], unique=False
    )
    op.create_index(
        "ix_analytics_events_occurred", "analytics_events", ["occurred_at"], unique=False
    )

    op.create_table(
        "analytics_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("anon_user_id", sa.String(length=64), nullable=True),
        sa.Column("book_id", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_s", sa.Float(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("pages_seen", sa.Integer(), nullable=False),
        sa.Column("deepest_page", sa.Integer(), nullable=True),
        sa.Column("words_read", sa.Integer(), nullable=False),
        sa.Column("completion_ratio", sa.Float(), nullable=True),
        sa.Column("dropoff_page", sa.Integer(), nullable=True),
        sa.Column("director_event_count", sa.Integer(), nullable=False),
        sa.Column("stall_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analytics_sessions")),
        sa.UniqueConstraint("session_id", name="uq_analytics_sessions_session_id"),
    )
    op.create_index(
        "ix_analytics_sessions_user", "analytics_sessions", ["anon_user_id"], unique=False
    )
    op.create_index(
        "ix_analytics_sessions_book_started",
        "analytics_sessions",
        ["book_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_analytics_sessions_started", "analytics_sessions", ["started_at"], unique=False
    )

    op.create_table(
        "analytics_daily_rollup",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("granularity", sa.String(length=16), nullable=False),
        sa.Column("bucket_label", sa.String(length=32), nullable=False),
        sa.Column("dimension_key", sa.String(length=128), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analytics_daily_rollup")),
        sa.UniqueConstraint(
            "bucket_start",
            "granularity",
            "dimension_key",
            "metric",
            name="uq_analytics_daily_rollup_grain",
        ),
    )
    op.create_index(
        "ix_analytics_daily_rollup_metric_bucket",
        "analytics_daily_rollup",
        ["metric", "bucket_start"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analytics_daily_rollup_metric_bucket", table_name="analytics_daily_rollup"
    )
    op.drop_table("analytics_daily_rollup")

    op.drop_index("ix_analytics_sessions_started", table_name="analytics_sessions")
    op.drop_index("ix_analytics_sessions_book_started", table_name="analytics_sessions")
    op.drop_index("ix_analytics_sessions_user", table_name="analytics_sessions")
    op.drop_table("analytics_sessions")

    op.drop_index("ix_analytics_events_occurred", table_name="analytics_events")
    op.drop_index("ix_analytics_events_session", table_name="analytics_events")
    op.drop_index("ix_analytics_events_name_occurred", table_name="analytics_events")
    op.drop_index("ix_analytics_events_user_occurred", table_name="analytics_events")
    op.drop_index("ix_analytics_events_book_occurred", table_name="analytics_events")
    op.drop_table("analytics_events")
