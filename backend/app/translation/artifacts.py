"""Persisted translation artifacts: ORM models + repository.

Translations are durable, keyed to source-content hashes (§8.7) so a re-read is
free and a Director edit only invalidates the affected segments. Four tables:

* ``translation_artifacts`` — one row per (book, target language, content kind):
  the umbrella record carrying the source language, glossary version, status,
  and aggregate cost. It is the thing the API lists ("this book is translated
  into French").
* ``translation_segments`` — the per-segment rows under an artifact: the source
  text, its content hash, the translated text, origin, quality, review flag.
  These ARE the translation memory's durable backing store; the in-process
  :class:`~app.translation.memory_store.TranslationMemory` is hydrated from them.
* ``translation_glossary`` — persisted glossary / do-not-translate entries per
  book (the canon's character names + agreed term renderings), versioned.
* ``translation_reviews`` — the human post-edit workflow rows (§review).

All FK to ``books.id`` with ``ondelete=CASCADE`` and follow the entity/shot model
conventions (string ids, JSONB for structured columns, named indexes).
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, StrIdMixin, TimestampMixin, new_id
from app.db.models.enums import str_enum
from app.db.repositories.base import BaseRepository

from .hashing import artifact_key
from .memory_store import MemoryEntry, TranslationMemory
from .types import ContentKind, TranslatedSegment


class ArtifactStatus(enum.StrEnum):
    """Lifecycle of a whole-book translation artifact."""

    DRAFT = "draft"  # being produced
    READY = "ready"  # all segments translated + above the review bar
    NEEDS_REVIEW = "needs_review"  # has segments flagged for post-edit
    STALE = "stale"  # source content changed; needs re-translation


class ReviewStatus(enum.StrEnum):
    """State of a per-segment human post-edit task (§review)."""

    PENDING = "pending"  # flagged, awaiting a reviewer
    IN_REVIEW = "in_review"  # claimed by a reviewer
    EDITED = "edited"  # a reviewer replaced the machine output
    APPROVED = "approved"  # accepted as-is (machine output was fine)
    REJECTED = "rejected"  # sent back for re-translation


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


class TranslationArtifact(StrIdMixin, TimestampMixin, Base):
    """One book × target-language × content-kind translation record."""

    __tablename__ = "translation_artifacts"
    __table_args__ = (
        UniqueConstraint("book_id", "target_lang", "content_kind"),
        Index("ix_translation_artifacts_book_lang", "book_id", "target_lang"),
    )

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_lang: Mapped[str] = mapped_column(String(32), nullable=False)
    target_lang: Mapped[str] = mapped_column(String(32), nullable=False)
    content_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    status: Mapped[ArtifactStatus] = mapped_column(
        str_enum(ArtifactStatus, "translation_artifact_status"),
        default=ArtifactStatus.DRAFT,
        nullable=False,
        index=True,
    )
    glossary_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rtl: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    segment_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    review_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # {"input_tokens", "output_tokens", "provider_calls", "cache_hits", ...}
    cost: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class TranslationSegment(StrIdMixin, TimestampMixin, Base):
    """One translated segment under an artifact (the durable TM backing store)."""

    __tablename__ = "translation_segments"
    __table_args__ = (
        UniqueConstraint("artifact_id", "segment_id"),
        Index("ix_translation_segments_artifact", "artifact_id"),
        # Exact-match cache lookup is by source hash within a language/kind.
        Index("ix_translation_segments_hash", "book_id", "target_lang", "source_hash"),
    )

    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("translation_artifacts.id", ondelete="CASCADE"), nullable=False
    )
    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False
    )
    segment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_lang: Mapped[str] = mapped_column(String(32), nullable=False)
    target_lang: Mapped[str] = mapped_column(String(32), nullable=False)
    content_kind: Mapped[str] = mapped_column(String(32), nullable=False)

    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    translated_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    translation_key_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    quality: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    warnings: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    glossary_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class TranslationGlossaryRow(StrIdMixin, TimestampMixin, Base):
    """A persisted glossary / do-not-translate entry, scoped to a book."""

    __tablename__ = "translation_glossary"
    __table_args__ = (
        UniqueConstraint("book_id", "source_term"),
        Index("ix_translation_glossary_book", "book_id"),
    )

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_term: Mapped[str] = mapped_column(String(512), nullable=False)
    # {target_lang: forced_translation}
    targets: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    do_not_translate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    case_sensitive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    whole_word: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Where the entry came from ("canon_character", "manual", "import").
    source_kind: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)


class TranslationReview(StrIdMixin, TimestampMixin, Base):
    """A human post-edit task for one low-confidence segment (§review)."""

    __tablename__ = "translation_reviews"
    __table_args__ = (
        UniqueConstraint("segment_row_id"),
        Index("ix_translation_reviews_book_status", "book_id", "status"),
    )

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False
    )
    segment_row_id: Mapped[str] = mapped_column(
        ForeignKey("translation_segments.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[ReviewStatus] = mapped_column(
        str_enum(ReviewStatus, "translation_review_status"),
        default=ReviewStatus.PENDING,
        nullable=False,
        index=True,
    )
    machine_text: Mapped[str] = mapped_column(Text, nullable=False)
    edited_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #


class TranslationRepo(BaseRepository):
    """Read/write translation artifacts, segments, glossary, reviews.

    Follows the repo contract: flushes (to surface constraint errors / make rows
    queryable) but never commits — the unit-of-work boundary owns the txn.
    """

    # -- artifacts -------------------------------------------------------- #

    async def get_artifact(
        self, *, book_id: str, target_lang: str, content_kind: ContentKind | str
    ) -> TranslationArtifact | None:
        kind = content_kind.value if isinstance(content_kind, ContentKind) else str(content_kind)
        stmt = select(TranslationArtifact).where(
            TranslationArtifact.book_id == book_id,
            TranslationArtifact.target_lang == target_lang,
            TranslationArtifact.content_kind == kind,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_artifacts(self, book_id: str) -> list[TranslationArtifact]:
        stmt = (
            select(TranslationArtifact)
            .where(TranslationArtifact.book_id == book_id)
            .order_by(TranslationArtifact.target_lang)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def upsert_artifact(
        self,
        *,
        book_id: str,
        source_lang: str,
        target_lang: str,
        content_kind: ContentKind | str,
        glossary_version: int,
        rtl: bool,
        status: ArtifactStatus,
        segment_count: int,
        review_count: int,
        cost: dict[str, Any] | None,
    ) -> TranslationArtifact:
        kind = content_kind.value if isinstance(content_kind, ContentKind) else str(content_kind)
        existing = await self.get_artifact(
            book_id=book_id, target_lang=target_lang, content_kind=kind
        )
        if existing is None:
            existing = TranslationArtifact(
                id=new_id(),
                book_id=book_id,
                source_lang=source_lang,
                target_lang=target_lang,
                content_kind=kind,
                artifact_hash=artifact_key(
                    book_id=book_id, target_lang=target_lang, content_kind=kind
                ),
            )
            self.session.add(existing)
        existing.source_lang = source_lang
        existing.glossary_version = glossary_version
        existing.rtl = rtl
        existing.status = status
        existing.segment_count = segment_count
        existing.review_count = review_count
        existing.cost = cost
        await self.session.flush()
        return existing

    # -- segments --------------------------------------------------------- #

    async def get_segment_by_key(
        self, *, book_id: str, target_lang: str, translation_key_hash: str
    ) -> TranslationSegment | None:
        stmt = select(TranslationSegment).where(
            TranslationSegment.book_id == book_id,
            TranslationSegment.target_lang == target_lang,
            TranslationSegment.translation_key_hash == translation_key_hash,
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def list_segments(self, artifact_id: str) -> list[TranslationSegment]:
        stmt = (
            select(TranslationSegment)
            .where(TranslationSegment.artifact_id == artifact_id)
            .order_by(TranslationSegment.segment_id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def upsert_segment(
        self,
        *,
        artifact_id: str,
        book_id: str,
        translated: TranslatedSegment,
        content_kind: ContentKind | str,
        source_hash: str,
        translation_key_hash: str,
        glossary_version: int,
    ) -> TranslationSegment:
        kind = content_kind.value if isinstance(content_kind, ContentKind) else str(content_kind)
        stmt = select(TranslationSegment).where(
            TranslationSegment.artifact_id == artifact_id,
            TranslationSegment.segment_id == translated.id,
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = TranslationSegment(
                id=new_id(), artifact_id=artifact_id, book_id=book_id, segment_id=translated.id
            )
            self.session.add(row)
        row.source_lang = translated.source_lang
        row.target_lang = translated.target_lang
        row.content_kind = kind
        row.source_text = translated.source_text
        row.translated_text = translated.translated_text
        row.source_hash = source_hash
        row.translation_key_hash = translation_key_hash
        row.origin = translated.origin.value
        row.quality = translated.quality
        row.needs_review = translated.needs_review
        row.warnings = list(translated.warnings) or None
        row.glossary_version = glossary_version
        await self.session.flush()
        return row

    async def hydrate_memory(
        self,
        memory: TranslationMemory,
        *,
        book_id: str,
        target_lang: str | None = None,
    ) -> int:
        """Load a book's persisted segments into an in-process TM. Returns count."""
        stmt = select(TranslationSegment).where(TranslationSegment.book_id == book_id)
        if target_lang is not None:
            stmt = stmt.where(TranslationSegment.target_lang == target_lang)
        rows: Sequence[TranslationSegment] = (await self.session.execute(stmt)).scalars().all()
        for row in rows:
            memory.put(
                MemoryEntry(
                    source_text=row.source_text,
                    translated_text=row.translated_text,
                    source_lang=row.source_lang,
                    target_lang=row.target_lang,
                    content_kind=ContentKind(row.content_kind),
                    glossary_version=row.glossary_version,
                    quality=row.quality,
                )
            )
        return len(rows)

    # -- glossary --------------------------------------------------------- #

    async def list_glossary(self, book_id: str) -> list[TranslationGlossaryRow]:
        stmt = (
            select(TranslationGlossaryRow)
            .where(TranslationGlossaryRow.book_id == book_id)
            .order_by(TranslationGlossaryRow.source_term)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def upsert_glossary_entry(
        self,
        *,
        book_id: str,
        source_term: str,
        targets: dict[str, str] | None = None,
        do_not_translate: bool = False,
        case_sensitive: bool = False,
        whole_word: bool = True,
        source_kind: str = "manual",
    ) -> TranslationGlossaryRow:
        stmt = select(TranslationGlossaryRow).where(
            TranslationGlossaryRow.book_id == book_id,
            TranslationGlossaryRow.source_term == source_term,
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = TranslationGlossaryRow(
                id=new_id(), book_id=book_id, source_term=source_term, version=1
            )
            self.session.add(row)
        else:
            row.version += 1
        row.targets = dict(targets) if targets else None
        row.do_not_translate = do_not_translate
        row.case_sensitive = case_sensitive
        row.whole_word = whole_word
        row.source_kind = source_kind
        await self.session.flush()
        return row

    async def add_glossary_entries(
        self, *, book_id: str, entries: Iterable[TranslationGlossaryRow]
    ) -> None:
        for entry in entries:
            entry.book_id = book_id
            self.session.add(entry)
        await self.session.flush()

    # -- reviews ---------------------------------------------------------- #

    async def create_review(
        self,
        *,
        book_id: str,
        segment_row_id: str,
        machine_text: str,
        quality: float,
        reason: str | None = None,
    ) -> TranslationReview:
        stmt = select(TranslationReview).where(
            TranslationReview.segment_row_id == segment_row_id
        )
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return existing
        review = TranslationReview(
            id=new_id(),
            book_id=book_id,
            segment_row_id=segment_row_id,
            machine_text=machine_text,
            quality=quality,
            reason=reason,
            status=ReviewStatus.PENDING,
        )
        self.session.add(review)
        await self.session.flush()
        return review

    async def get_review(self, review_id: str) -> TranslationReview | None:
        return await self.session.get(TranslationReview, review_id)

    async def list_reviews(
        self, book_id: str, *, status: ReviewStatus | None = None
    ) -> list[TranslationReview]:
        stmt = select(TranslationReview).where(TranslationReview.book_id == book_id)
        if status is not None:
            stmt = stmt.where(TranslationReview.status == status)
        stmt = stmt.order_by(TranslationReview.created_at)
        return list((await self.session.execute(stmt)).scalars())


__all__ = [
    "ArtifactStatus",
    "ReviewStatus",
    "TranslationArtifact",
    "TranslationGlossaryRow",
    "TranslationRepo",
    "TranslationReview",
    "TranslationSegment",
]
