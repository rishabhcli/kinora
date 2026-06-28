"""``report_artifacts`` — persisted, signed-retrievable rendered reports.

A generated report is rendered once to one or more formats and each rendered
blob is stored in object storage; this table is the **index** over those blobs:
who it belongs to, what it describes, which format, where the bytes live, and the
content hash that lets a re-request dedup to the existing artifact (the same
"a re-read costs nothing" idea as the shot cache, §8.7, applied to documents).

The row never holds the bytes — only the object-store ``storage_key`` and a
``sha256`` of the rendered content. Retrieval is a signed/presigned URL minted by
:class:`~app.storage.object_store.ObjectStore`, so a report download is a
short-lived link, never a public path.

Additive-only: a new table on its own migration; no existing table is touched.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, StrIdMixin, TimestampMixin
from app.db.models.enums import str_enum


class ReportKind(enum.StrEnum):
    """The report kinds the builders produce (drives source + audience)."""

    # Reader-facing.
    READING_PROGRESS = "reading_progress"
    COMPLETION_CERTIFICATE = "completion_certificate"
    YEAR_IN_REVIEW = "year_in_review"
    HIGHLIGHTS_DIGEST = "highlights_digest"
    # Operator-facing.
    BUDGET = "budget"
    QUALITY = "quality"
    RENDER_THROUGHPUT = "render_throughput"
    LIBRARY_OVERVIEW = "library_overview"


class ReportAudience(enum.StrEnum):
    """Who a report is for — gates which kinds a non-admin user may request."""

    READER = "reader"
    OPERATOR = "operator"


class ReportFormatEnum(enum.StrEnum):
    """The persisted output format of an artifact (mirrors render.ReportFormat)."""

    PDF = "pdf"
    HTML = "html"
    CSV = "csv"
    JSON = "json"


class ReportStatus(enum.StrEnum):
    """Lifecycle of a report artifact row."""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class ReportArtifact(StrIdMixin, TimestampMixin, Base):
    """An index row over one rendered, stored report blob."""

    __tablename__ = "report_artifacts"
    __table_args__ = (
        Index("ix_report_artifacts_owner", "user_id", "kind"),
        Index("ix_report_artifacts_book", "book_id"),
        # Dedup / idempotency: the same logical report+format renders to one row.
        Index("ix_report_artifacts_dedup", "content_hash", "format"),
        Index("ix_report_artifacts_subject", "subject_kind", "subject_id"),
    )

    #: Owner of the report (the reader, or the operator who ran it). SET NULL on
    #: user deletion so an operator audit trail survives the account.
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Optional book the report is about (reader reports + per-book operator ones).
    book_id: Mapped[str | None] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), nullable=True
    )

    kind: Mapped[ReportKind] = mapped_column(
        str_enum(ReportKind, "report_kind"), nullable=False
    )
    audience: Mapped[ReportAudience] = mapped_column(
        str_enum(ReportAudience, "report_audience"), nullable=False
    )
    format: Mapped[ReportFormatEnum] = mapped_column(
        str_enum(ReportFormatEnum, "report_format"), nullable=False
    )
    status: Mapped[ReportStatus] = mapped_column(
        str_enum(ReportStatus, "report_status"),
        default=ReportStatus.READY,
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    #: A free-form subject key so a report can describe a non-book subject
    #: (e.g. a calendar year for year-in-review, "global" for fleet operator).
    subject_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: Object-store key the rendered bytes live at (never the bytes themselves).
    storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    #: sha256 of the rendered bytes — dedup + integrity.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    #: How the report came to be: ``on_demand`` | ``scheduled`` | ``cli``.
    trigger: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Optional structured params the report was built from (window, year, …).
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: Optional expiry for retention sweeps (NULL = keep).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Error detail when ``status == FAILED``.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = [
    "ReportArtifact",
    "ReportAudience",
    "ReportFormatEnum",
    "ReportKind",
    "ReportStatus",
]
