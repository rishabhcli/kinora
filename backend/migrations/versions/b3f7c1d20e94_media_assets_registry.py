"""media_assets — the per-asset media registry (Media domain; §8.7, §9, §12).

Adds the ``media_assets`` table: one durable row per managed media blob with its
object-store key, the byte-level content hash (the §8.7 dedup key, one level
below the shot-hash), MIME type, size, geometry/duration for AV assets, a
free-form ``meta`` (sprite geometry, HLS variants, …), a reference count for the
lifecycle GC, and an optional retention horizon.

Purely additive: it touches no existing table and chains on the current head.
Complements the §9.7 render pipeline (which already persists provider videos)
rather than rewriting it.

Revision ID: b3f7c1d20e94
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b3f7c1d20e94"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The MediaAssetKind values (kept in sync with app.media.kinds.MediaAssetKind).
_MEDIA_KINDS = (
    "clip",
    "scene",
    "poster",
    "thumbnail",
    "sprite",
    "vtt",
    "hls",
    "dash",
    "audio",
    "keyframe",
    "source",
    "other",
)


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=True),
        sa.Column(
            "kind",
            sa.Enum(*_MEDIA_KINDS, name="media_asset_kind", native_enum=False),
            nullable=False,
        ),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=True),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("etag", sa.String(length=256), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ref_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
            name=op.f("fk_media_assets_book_id_books"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_assets")),
    )
    op.create_index(op.f("ix_media_assets_book_id"), "media_assets", ["book_id"], unique=False)
    op.create_index(op.f("ix_media_assets_kind"), "media_assets", ["kind"], unique=False)
    op.create_index(
        op.f("ix_media_assets_content_hash"), "media_assets", ["content_hash"], unique=False
    )
    op.create_index(
        "ix_media_assets_book_kind", "media_assets", ["book_id", "kind"], unique=False
    )
    op.create_index(
        "ix_media_assets_kind_expires", "media_assets", ["kind", "expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_media_assets_kind_expires", table_name="media_assets")
    op.drop_index("ix_media_assets_book_kind", table_name="media_assets")
    op.drop_index(op.f("ix_media_assets_content_hash"), table_name="media_assets")
    op.drop_index(op.f("ix_media_assets_kind"), table_name="media_assets")
    op.drop_index(op.f("ix_media_assets_book_id"), table_name="media_assets")
    op.drop_table("media_assets")
