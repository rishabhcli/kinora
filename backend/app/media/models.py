"""``media_assets`` — the per-asset registry (kinora.md §8.7, §9, §12).

One durable row per managed media blob: its object-store key, the byte-level
content hash (the §8.7 dedup key — one level below the shot-hash), MIME type,
size, geometry/duration for AV assets, a free-form ``meta`` (sprite geometry,
HLS variants, …), a reference count for GC, and an optional retention horizon.

This **complements** the §9.7 render pipeline (which writes clips at
``clips/{book}/{shot}.mp4`` and signs URLs ad-hoc) by giving the system a single
queryable inventory of every blob it stores — what derived assets exist, what
they cost, when they may be collected — without changing the render path.

The model lives in :mod:`app.media` and is re-exported from
:mod:`app.db.models` so Alembic autogenerate and relationship resolution see it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, StrIdMixin, TimestampMixin
from app.db.models.enums import str_enum
from app.media.kinds import MediaAssetKind


class MediaAsset(StrIdMixin, TimestampMixin, Base):
    """A managed media blob and everything the system knows about it."""

    __tablename__ = "media_assets"
    __table_args__ = (
        # The dedup probe: "do we already have this blob?" by content hash.
        Index("ix_media_assets_book_kind", "book_id", "kind"),
        # The GC sweep: find collectible/expired derived assets fast.
        Index("ix_media_assets_kind_expires", "kind", "expires_at"),
    )

    book_id: Mapped[str | None] = mapped_column(
        ForeignKey("books.id", ondelete="SET NULL"), index=True, nullable=True
    )
    kind: Mapped[MediaAssetKind] = mapped_column(
        str_enum(MediaAssetKind, "media_asset_kind"),
        default=MediaAssetKind.OTHER,
        nullable=False,
        index=True,
    )
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    content_type: Mapped[str] = mapped_column(
        String(128), default="application/octet-stream", nullable=False
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    etag: Mapped[str | None] = mapped_column(String(256), nullable=True)
    #: Free-form derived facts: sprite columns/rows, HLS variants, parent key, …
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: Live references; the lifecycle GC only collects rows at zero.
    ref_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Retention horizon; ``NULL`` means "keep indefinitely".
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["MediaAsset"]
