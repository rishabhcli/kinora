"""integrations — app_connections, imported_items, sync_runs

Revision ID: i7a1b2c3d4e5
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28 17:00:00.000000

Adds the three tables behind the third-party-import framework
(``app.integrations``):

* ``app_connections`` — one per (user, provider): sealed token blob, status,
  scopes, connector config, incremental cursor, and health counters.
* ``imported_items`` — the dedup ledger; ``UNIQUE(connection_id,
  source_item_id)`` makes a source item import exactly once, with a stored
  content hash so a changed item can be re-imported.
* ``sync_runs`` — append-only run history (counts, status, error, timing).

Purely additive: no existing table is touched. Enum columns are stored as
VARCHAR + CHECK (the project convention, ``native_enum=False``) so no Postgres
ENUM type is created.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "i7a1b2c3d4e5"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONNECTION_STATUS = ("active", "pending", "needs_reauth", "error", "disconnected")
_SYNC_RUN_STATUS = ("running", "success", "partial", "failed")


def upgrade() -> None:
    op.create_table(
        "app_connections",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("account_label", sa.String(length=512), nullable=True),
        sa.Column(
            "status",
            sa.Enum(*_CONNECTION_STATUS, name="connection_status", native_enum=False),
            nullable=False,
        ),
        sa.Column("sealed_token", sa.Text(), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cursor_watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor_etag", sa.String(length=512), nullable=True),
        sa.Column("cursor_opaque", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_app_connections_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_connections")),
    )
    op.create_index(
        op.f("ix_app_connections_user_id"), "app_connections", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_app_connections_status"), "app_connections", ["status"], unique=False
    )
    op.create_index(
        "ix_app_connections_user_provider", "app_connections", ["user_id", "provider"], unique=False
    )

    op.create_table(
        "imported_items",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("connection_id", sa.String(length=64), nullable=False),
        sa.Column("source_item_id", sa.String(length=512), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["app_connections.id"],
            name=op.f("fk_imported_items_connection_id_app_connections"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["book_id"], ["books.id"], name=op.f("fk_imported_items_book_id_books"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_imported_items")),
        sa.UniqueConstraint(
            "connection_id", "source_item_id", name="uq_imported_items_conn_source"
        ),
    )
    op.create_index(
        op.f("ix_imported_items_connection_id"), "imported_items", ["connection_id"], unique=False
    )
    op.create_index("ix_imported_items_book", "imported_items", ["book_id"], unique=False)

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("connection_id", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*_SYNC_RUN_STATUS, name="sync_run_status", native_enum=False),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("items_seen", sa.Integer(), nullable=False),
        sa.Column("items_imported", sa.Integer(), nullable=False),
        sa.Column("items_skipped", sa.Integer(), nullable=False),
        sa.Column("items_failed", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["app_connections.id"],
            name=op.f("fk_sync_runs_connection_id_app_connections"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_runs")),
    )
    op.create_index(op.f("ix_sync_runs_connection_id"), "sync_runs", ["connection_id"], unique=False)
    op.create_index(op.f("ix_sync_runs_status"), "sync_runs", ["status"], unique=False)
    op.create_index("ix_sync_runs_conn_created", "sync_runs", ["connection_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sync_runs_conn_created", table_name="sync_runs")
    op.drop_index(op.f("ix_sync_runs_status"), table_name="sync_runs")
    op.drop_index(op.f("ix_sync_runs_connection_id"), table_name="sync_runs")
    op.drop_table("sync_runs")

    op.drop_index("ix_imported_items_book", table_name="imported_items")
    op.drop_index(op.f("ix_imported_items_connection_id"), table_name="imported_items")
    op.drop_table("imported_items")

    op.drop_index("ix_app_connections_user_provider", table_name="app_connections")
    op.drop_index(op.f("ix_app_connections_status"), table_name="app_connections")
    op.drop_index(op.f("ix_app_connections_user_id"), table_name="app_connections")
    op.drop_table("app_connections")
