"""``ingest_checkpoints`` — the durable Phase-A milestone ledger (§9.1, additive).

[Agent: ingest-domain — additive shared-file change, documented in
``app/ingest/DESIGN.md``.]

§9.1 says "a partial import is resumable, and extraction is idempotent". This
table makes that *checkpointed*: each heavy ingest stage (extract, analyse,
canon, shot-plan, identity-lock) records a row here once it completes, so a
crashed/restarted ingest can skip the stages it already finished instead of
recomputing them. One row per ``(book_id, milestone)`` (a unique constraint),
carrying the completion time and a small JSONB payload of stage telemetry
(counts) for observability.

The pipeline degrades gracefully when this table is absent (treated as "no
checkpoints recorded"), so a deployment that has not yet migrated still works —
it simply re-runs every stage like before.
"""

from __future__ import annotations

import enum
from typing import Any

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, StrIdMixin, TimestampMixin
from app.db.models.enums import str_enum


class IngestMilestone(enum.StrEnum):
    """The checkpointable units-of-work of Phase-A ingest (in pipeline order)."""

    EXTRACT = "extract"
    ANALYZE = "analyze"
    CANON = "canon"
    SHOT_PLAN = "shot_plan"
    IDENTITY_LOCK = "identity_lock"


class IngestCheckpoint(StrIdMixin, TimestampMixin, Base):
    """A completed Phase-A milestone for a book (the resume ledger)."""

    __tablename__ = "ingest_checkpoints"
    __table_args__ = (UniqueConstraint("book_id", "milestone", name="uq_ingest_checkpoint"),)

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), nullable=False, index=True
    )
    milestone: Mapped[IngestMilestone] = mapped_column(
        str_enum(IngestMilestone, "ingest_milestone"), nullable=False
    )
    #: Optional stage telemetry (e.g. ``{"num_pages": 312, "total_words": 84120}``).
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


__all__ = ["IngestCheckpoint", "IngestMilestone"]
