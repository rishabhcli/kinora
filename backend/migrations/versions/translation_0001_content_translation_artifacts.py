"""Content translation: artifacts, segments, glossary, reviews

Revision ID: c1d2e3f4a5b6
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28

Additive migration for the content-translation subsystem (``app.translation``,
kinora.md §8/§9). Creates four new tables and touches no existing one:

* ``translation_artifacts`` — one row per (book, target language, content kind):
  the umbrella translation record (source language, status, glossary version,
  aggregate cost).
* ``translation_segments`` — the per-segment translations (source text + hash,
  translated text, origin, quality, review flag). These are the durable backing
  store of the translation memory, keyed to source-content hashes (§8.7) so a
  re-read is free.
* ``translation_glossary`` — versioned glossary / do-not-translate terms per book
  (the canon character names + agreed renderings).
* ``translation_reviews`` — the human post-edit workflow rows.

Status columns are portable VARCHAR + named CHECK (``native_enum=False``), matching
the rest of the schema. The enum *types* are created inline with ``create_type``
disabled, so this migration owns no Postgres ENUM object to drop.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "translation_0001"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ARTIFACT_STATUS = sa.Enum(
    "draft",
    "ready",
    "needs_review",
    "stale",
    name="translation_artifact_status",
    native_enum=False,
    create_constraint=True,
)
_REVIEW_STATUS = sa.Enum(
    "pending",
    "in_review",
    "edited",
    "approved",
    "rejected",
    name="translation_review_status",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    # -- translation_artifacts -------------------------------------------- #
    op.create_table(
        "translation_artifacts",
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("source_lang", sa.String(length=32), nullable=False),
        sa.Column("target_lang", sa.String(length=32), nullable=False),
        sa.Column("content_kind", sa.String(length=32), nullable=False),
        sa.Column("artifact_hash", sa.String(length=64), nullable=False),
        sa.Column("status", _ARTIFACT_STATUS, nullable=False),
        sa.Column("glossary_version", sa.Integer(), nullable=False),
        sa.Column("rtl", sa.Boolean(), nullable=False),
        sa.Column("segment_count", sa.Integer(), nullable=False),
        sa.Column("review_count", sa.Integer(), nullable=False),
        sa.Column("cost", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
            name=op.f("fk_translation_artifacts_book_id_books"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_translation_artifacts")),
        sa.UniqueConstraint(
            "book_id",
            "target_lang",
            "content_kind",
            name=op.f("uq_translation_artifacts_book_id_target_lang_content_kind"),
        ),
    )
    op.create_index(
        op.f("ix_translation_artifacts_book_id"),
        "translation_artifacts",
        ["book_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_translation_artifacts_artifact_hash"),
        "translation_artifacts",
        ["artifact_hash"],
        unique=False,
    )
    op.create_index(
        op.f("ix_translation_artifacts_status"),
        "translation_artifacts",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_translation_artifacts_book_lang",
        "translation_artifacts",
        ["book_id", "target_lang"],
        unique=False,
    )

    # -- translation_segments --------------------------------------------- #
    op.create_table(
        "translation_segments",
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("segment_id", sa.String(length=128), nullable=False),
        sa.Column("source_lang", sa.String(length=32), nullable=False),
        sa.Column("target_lang", sa.String(length=32), nullable=False),
        sa.Column("content_kind", sa.String(length=32), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("translated_text", sa.Text(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("translation_key_hash", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("quality", sa.Float(), nullable=False),
        sa.Column("needs_review", sa.Boolean(), nullable=False),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("glossary_version", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["translation_artifacts.id"],
            name=op.f("fk_translation_segments_artifact_id_translation_artifacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
            name=op.f("fk_translation_segments_book_id_books"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_translation_segments")),
        sa.UniqueConstraint(
            "artifact_id",
            "segment_id",
            name=op.f("uq_translation_segments_artifact_id_segment_id"),
        ),
    )
    op.create_index(
        op.f("ix_translation_segments_book_id"),
        "translation_segments",
        ["book_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_translation_segments_translation_key_hash"),
        "translation_segments",
        ["translation_key_hash"],
        unique=False,
    )
    op.create_index(
        "ix_translation_segments_artifact",
        "translation_segments",
        ["artifact_id"],
        unique=False,
    )
    op.create_index(
        "ix_translation_segments_hash",
        "translation_segments",
        ["book_id", "target_lang", "source_hash"],
        unique=False,
    )

    # -- translation_glossary --------------------------------------------- #
    op.create_table(
        "translation_glossary",
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("source_term", sa.String(length=512), nullable=False),
        sa.Column("targets", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("do_not_translate", sa.Boolean(), nullable=False),
        sa.Column("case_sensitive", sa.Boolean(), nullable=False),
        sa.Column("whole_word", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
            name=op.f("fk_translation_glossary_book_id_books"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_translation_glossary")),
        sa.UniqueConstraint(
            "book_id",
            "source_term",
            name=op.f("uq_translation_glossary_book_id_source_term"),
        ),
    )
    op.create_index(
        op.f("ix_translation_glossary_book_id"),
        "translation_glossary",
        ["book_id"],
        unique=False,
    )
    op.create_index(
        "ix_translation_glossary_book",
        "translation_glossary",
        ["book_id"],
        unique=False,
    )

    # -- translation_reviews ---------------------------------------------- #
    op.create_table(
        "translation_reviews",
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("segment_row_id", sa.String(length=64), nullable=False),
        sa.Column("status", _REVIEW_STATUS, nullable=False),
        sa.Column("machine_text", sa.Text(), nullable=False),
        sa.Column("edited_text", sa.Text(), nullable=True),
        sa.Column("reviewer_id", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("quality", sa.Float(), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
            name=op.f("fk_translation_reviews_book_id_books"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["segment_row_id"],
            ["translation_segments.id"],
            name=op.f("fk_translation_reviews_segment_row_id_translation_segments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_translation_reviews")),
        sa.UniqueConstraint(
            "segment_row_id",
            name=op.f("uq_translation_reviews_segment_row_id"),
        ),
    )
    op.create_index(
        op.f("ix_translation_reviews_book_id"),
        "translation_reviews",
        ["book_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_translation_reviews_status"),
        "translation_reviews",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_translation_reviews_book_status",
        "translation_reviews",
        ["book_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_translation_reviews_book_status", table_name="translation_reviews")
    op.drop_index(op.f("ix_translation_reviews_status"), table_name="translation_reviews")
    op.drop_index(op.f("ix_translation_reviews_book_id"), table_name="translation_reviews")
    op.drop_table("translation_reviews")

    op.drop_index("ix_translation_glossary_book", table_name="translation_glossary")
    op.drop_index(op.f("ix_translation_glossary_book_id"), table_name="translation_glossary")
    op.drop_table("translation_glossary")

    op.drop_index("ix_translation_segments_hash", table_name="translation_segments")
    op.drop_index("ix_translation_segments_artifact", table_name="translation_segments")
    op.drop_index(
        op.f("ix_translation_segments_translation_key_hash"), table_name="translation_segments"
    )
    op.drop_index(op.f("ix_translation_segments_book_id"), table_name="translation_segments")
    op.drop_table("translation_segments")

    op.drop_index("ix_translation_artifacts_book_lang", table_name="translation_artifacts")
    op.drop_index(op.f("ix_translation_artifacts_status"), table_name="translation_artifacts")
    op.drop_index(
        op.f("ix_translation_artifacts_artifact_hash"), table_name="translation_artifacts"
    )
    op.drop_index(op.f("ix_translation_artifacts_book_id"), table_name="translation_artifacts")
    op.drop_table("translation_artifacts")
