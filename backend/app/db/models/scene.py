"""``scenes`` — narrative units (usually one to two pages), the stitch boundary.

A scene is the §4.2 unit between *book* and *shot*: the boundary at which
accepted shots are concatenated (§9.6). Each scene names the ``Style`` canon
node that governs its look (``style_entity_key``); when null, ``canon.query``
falls back to the book's default style so the palette/lens/art-direction is a
retrieved constant rather than a per-shot whim (§8.1).
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin


class Scene(StrIdMixin, CreatedAtMixin, Base):
    """One narrative scene of a book (the stitching boundary)."""

    __tablename__ = "scenes"
    __table_args__ = (
        UniqueConstraint("book_id", "scene_index"),
        Index("ix_scenes_book_index", "book_id", "scene_index"),
    )

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scene_index: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)

    # The Style canon node (entity_key) governing this scene; NULL => book default.
    style_entity_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
