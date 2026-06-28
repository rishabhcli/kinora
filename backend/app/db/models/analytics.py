"""Product-analytics tables — the durable event log + derived summaries.

Three tables back :mod:`app.analytics`:

* ``analytics_events`` — one row per **scrubbed** event (the canonical
  :class:`~app.analytics.events.TrackedEvent`). ``event_id`` is UNIQUE so the
  batched ingest is idempotent (a retried batch is a no-op). PII never lands
  here: identifiers arrive already pseudonymised and props are allow-listed.
* ``analytics_sessions`` — one row per derived reading session (the output of
  gap-based sessionization). ``session_id`` is UNIQUE; recomputing overwrites.
* ``analytics_daily_rollup`` — pre-aggregated summary rows at the
  ``(bucket_start, granularity, dimension_key, metric)`` grain. The UNIQUE on
  that tuple makes the rollup upsert idempotent.

These tables are deliberately *not* foreign-keyed to ``books``/``users`` — the
event log must survive a book/user deletion (analytics is historical) and
``anon_user_id`` is a pseudonym, not a user PK. ``book_id`` is stored as a plain
string for slicing only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin


class AnalyticsEvent(StrIdMixin, CreatedAtMixin, Base):
    """One scrubbed product-analytics event (idempotent on ``event_id``)."""

    __tablename__ = "analytics_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_analytics_events_event_id"),
        Index("ix_analytics_events_book_occurred", "book_id", "occurred_at"),
        Index("ix_analytics_events_user_occurred", "anon_user_id", "occurred_at"),
        Index("ix_analytics_events_name_occurred", "name", "occurred_at"),
        Index("ix_analytics_events_session", "session_key"),
        Index("ix_analytics_events_occurred", "occurred_at"),
    )

    # Client-supplied idempotency key (distinct from the row's surrogate ``id``).
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    anon_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    book_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    props: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class AnalyticsSession(StrIdMixin, CreatedAtMixin, Base):
    """A derived reading session (sessionization output; UNIQUE ``session_id``)."""

    __tablename__ = "analytics_sessions"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_analytics_sessions_session_id"),
        Index("ix_analytics_sessions_user", "anon_user_id"),
        Index("ix_analytics_sessions_book_started", "book_id", "started_at"),
        Index("ix_analytics_sessions_started", "started_at"),
    )

    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    anon_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    book_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_s: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pages_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deepest_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    words_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    dropoff_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    director_event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stall_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AnalyticsDailyRollup(StrIdMixin, CreatedAtMixin, Base):
    """A pre-aggregated summary row (idempotent on the grain tuple)."""

    __tablename__ = "analytics_daily_rollup"
    __table_args__ = (
        UniqueConstraint(
            "bucket_start",
            "granularity",
            "dimension_key",
            "metric",
            name="uq_analytics_daily_rollup_grain",
        ),
        Index("ix_analytics_daily_rollup_metric_bucket", "metric", "bucket_start"),
    )

    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    granularity: Mapped[str] = mapped_column(String(16), nullable=False)
    bucket_label: Mapped[str] = mapped_column(String(32), nullable=False)
    dimension_key: Mapped[str] = mapped_column(String(128), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


__all__ = ["AnalyticsDailyRollup", "AnalyticsEvent", "AnalyticsSession"]
