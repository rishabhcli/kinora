"""``books`` and ``pages`` — the imported work and its extracted pages.

``pages.word_boxes`` holds the per-word geometry produced by the PyMuPDF
extract step: a list of ``{word_index, text, bbox:[x, y, w, h]}`` with the bbox
normalised to ``[0, 1]`` page coordinates so the karaoke highlight layer can
paint it and the ``word_index`` ties back into the source-span index (§9.4).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin, TimestampMixin
from app.db.models.enums import BookStatus, str_enum


class Book(StrIdMixin, TimestampMixin, Base):
    """A book and its import status; per-book scheduler/budget overrides."""

    __tablename__ = "books"

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    author: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_pdf_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[BookStatus] = mapped_column(
        str_enum(BookStatus, "book_status"),
        default=BookStatus.IMPORTING,
        nullable=False,
        index=True,
    )
    num_pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    art_direction: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Per-book overrides of the global watermark/horizon defaults (seconds).
    watermark_low_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    watermark_high_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    commit_horizon_s: Mapped[float | None] = mapped_column(Float, nullable=True)


class Page(StrIdMixin, CreatedAtMixin, Base):
    """One rendered page: image, extracted text, and per-word boxes."""

    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("book_id", "page_number"),)

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    image_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    word_boxes: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
