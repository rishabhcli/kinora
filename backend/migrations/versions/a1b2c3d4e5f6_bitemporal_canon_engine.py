"""Bitemporal canon engine: bitemporal_states, canon_audit, canon_branches

Revision ID: a1b2c3d4e5f6
Revises: f1c2a3b4d5e6
Create Date: 2026-06-28

Additive migration that turns the canon into a bitemporal knowledge graph (kinora.md §8).
It creates three new tables and touches no existing one:

* ``bitemporal_states`` — continuity facts carrying BOTH a valid-time beat interval
  (the story timeline, §8.5) AND a transaction-time UTC interval (when the system believed
  it), plus a ``branch`` and a CRDT write-stamp. This is what makes "canon as of any past
  write" and conflict-free concurrent edits possible.
* ``canon_audit`` — an append-only, hash-chained log of every canon mutation (tamper-evident).
* ``canon_branches`` — the branch registry for FORK / DIFF / MERGE.

The existing uni-temporal ``entities`` / ``continuity_states`` path is unaffected; the
bitemporal engine is a parallel, opt-in store written only by the new MCP tools.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "f1c2a3b4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bitemporal_states",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("fact_key", sa.String(length=128), nullable=False),
        sa.Column("branch", sa.String(length=128), nullable=False),
        sa.Column("subject_entity_key", sa.String(length=128), nullable=False),
        sa.Column("predicate", sa.String(length=256), nullable=False),
        sa.Column("object_value", sa.Text(), nullable=False),
        sa.Column("valid_from_beat", sa.Integer(), nullable=False),
        sa.Column("valid_to_beat", sa.Integer(), nullable=True),
        sa.Column("tx_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tx_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stamp_wall", sa.BigInteger(), nullable=False),
        sa.Column("stamp_counter", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("source_span", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
            name=op.f("fk_bitemporal_states_book_id_books"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bitemporal_states")),
    )
    op.create_index(
        op.f("ix_bitemporal_states_book_id"), "bitemporal_states", ["book_id"], unique=False
    )
    op.create_index(
        "ix_bitemporal_states_read",
        "bitemporal_states",
        ["book_id", "branch", "subject_entity_key", "valid_from_beat", "valid_to_beat"],
        unique=False,
    )
    op.create_index(
        "ix_bitemporal_states_fact_key",
        "bitemporal_states",
        ["book_id", "fact_key"],
        unique=False,
    )
    op.create_index(
        "ix_bitemporal_states_branch",
        "bitemporal_states",
        ["book_id", "branch"],
        unique=False,
    )
    op.create_index(
        "ix_bitemporal_states_tx",
        "bitemporal_states",
        ["book_id", "branch", "tx_to"],
        unique=False,
    )

    op.create_table(
        "canon_audit",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("branch", sa.String(length=128), nullable=False),
        sa.Column(
            "action",
            sa.Enum(
                "assert_fact",
                "correct_fact",
                "retire_fact",
                "fork_branch",
                "merge_branch",
                "upsert_entity",
                name="canon_audit_action",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("target_key", sa.String(length=128), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("prev_hash", sa.String(length=64), nullable=True),
        sa.Column("entry_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
            name=op.f("fk_canon_audit_book_id_books"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_canon_audit")),
        sa.UniqueConstraint("book_id", "seq", name="uq_canon_audit_book_id_seq"),
    )
    op.create_index(op.f("ix_canon_audit_book_id"), "canon_audit", ["book_id"], unique=False)
    op.create_index("ix_canon_audit_book_seq", "canon_audit", ["book_id", "seq"], unique=False)
    op.create_index("ix_canon_audit_branch", "canon_audit", ["book_id", "branch"], unique=False)

    op.create_table(
        "canon_branches",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("parent", sa.String(length=128), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "open",
                "merged",
                "abandoned",
                name="canon_branch_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("base_beat", sa.Integer(), nullable=True),
        sa.Column("base_tx", sa.DateTime(timezone=True), nullable=True),
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
            name=op.f("fk_canon_branches_book_id_books"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_canon_branches")),
        sa.UniqueConstraint("book_id", "name", name="uq_canon_branches_book_id_name"),
    )
    op.create_index(op.f("ix_canon_branches_book_id"), "canon_branches", ["book_id"], unique=False)
    op.create_index("ix_canon_branches_book", "canon_branches", ["book_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_canon_branches_book", table_name="canon_branches")
    op.drop_index(op.f("ix_canon_branches_book_id"), table_name="canon_branches")
    op.drop_table("canon_branches")

    op.drop_index("ix_canon_audit_branch", table_name="canon_audit")
    op.drop_index("ix_canon_audit_book_seq", table_name="canon_audit")
    op.drop_index(op.f("ix_canon_audit_book_id"), table_name="canon_audit")
    op.drop_table("canon_audit")

    op.drop_index("ix_bitemporal_states_tx", table_name="bitemporal_states")
    op.drop_index("ix_bitemporal_states_branch", table_name="bitemporal_states")
    op.drop_index("ix_bitemporal_states_fact_key", table_name="bitemporal_states")
    op.drop_index("ix_bitemporal_states_read", table_name="bitemporal_states")
    op.drop_index(op.f("ix_bitemporal_states_book_id"), table_name="bitemporal_states")
    op.drop_table("bitemporal_states")
