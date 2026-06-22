"""Phase 4 memory layer: scenes, beats, budget_ledger

Revision ID: b7d4e9a1c3f2
Revises: 0426a4023e7c
Create Date: 2026-06-22 18:20:00.000000

Additive migration stacked on the Phase 2 initial schema. Adds the scene/beat
planning units (kinora.md §4.2) and the append-only budget ledger (§11.1) the
MCP canon-memory service is built on. It touches no existing table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b7d4e9a1c3f2"
down_revision: str | None = "0426a4023e7c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scenes",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("scene_index", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("style_entity_key", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["book_id"], ["books.id"], name=op.f("fk_scenes_book_id_books"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scenes")),
        sa.UniqueConstraint("book_id", "scene_index", name=op.f("uq_scenes_book_id_scene_index")),
    )
    op.create_index(op.f("ix_scenes_book_id"), "scenes", ["book_id"], unique=False)
    op.create_index("ix_scenes_book_index", "scenes", ["book_id", "scene_index"], unique=False)

    op.create_table(
        "beats",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("scene_id", sa.String(length=64), nullable=False),
        sa.Column("beat_index", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("entities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("described_visuals", sa.Text(), nullable=True),
        sa.Column("mood", sa.Text(), nullable=True),
        sa.Column("source_span", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["book_id"], ["books.id"], name=op.f("fk_beats_book_id_books"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["scene_id"], ["scenes.id"], name=op.f("fk_beats_scene_id_scenes"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_beats")),
        sa.UniqueConstraint("book_id", "beat_index", name=op.f("uq_beats_book_id_beat_index")),
    )
    op.create_index(op.f("ix_beats_book_id"), "beats", ["book_id"], unique=False)
    op.create_index(op.f("ix_beats_scene_id"), "beats", ["scene_id"], unique=False)
    op.create_index(
        "ix_beats_book_scene_index", "beats", ["book_id", "scene_id", "beat_index"], unique=False
    )

    op.create_table(
        "budget_ledger",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("scene_id", sa.String(length=64), nullable=True),
        sa.Column(
            "kind",
            sa.Enum("reserve", "commit", "release", name="budget_kind", native_enum=False),
            nullable=False,
        ),
        sa.Column("video_seconds", sa.Float(), nullable=False),
        sa.Column("reservation_id", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
            name=op.f("fk_budget_ledger_book_id_books"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_budget_ledger_session_id_sessions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_ledger")),
    )
    op.create_index(
        "ix_budget_ledger_scope",
        "budget_ledger",
        ["book_id", "session_id", "scene_id"],
        unique=False,
    )
    op.create_index(
        "ix_budget_ledger_reservation", "budget_ledger", ["reservation_id"], unique=False
    )
    op.create_index("ix_budget_ledger_kind", "budget_ledger", ["kind"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_budget_ledger_kind", table_name="budget_ledger")
    op.drop_index("ix_budget_ledger_reservation", table_name="budget_ledger")
    op.drop_index("ix_budget_ledger_scope", table_name="budget_ledger")
    op.drop_table("budget_ledger")

    op.drop_index("ix_beats_book_scene_index", table_name="beats")
    op.drop_index(op.f("ix_beats_scene_id"), table_name="beats")
    op.drop_index(op.f("ix_beats_book_id"), table_name="beats")
    op.drop_table("beats")

    op.drop_index("ix_scenes_book_index", table_name="scenes")
    op.drop_index(op.f("ix_scenes_book_id"), table_name="scenes")
    op.drop_table("scenes")
