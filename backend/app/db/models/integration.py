"""Integration tables — app connections, the dedup ledger, and sync-run history.

These back the third-party-import framework (``app.integrations``):

* :class:`AppConnection` — one row per (user, provider) connection. Holds the
  *sealed* token blob (never plaintext; see :mod:`app.integrations.crypto`), the
  connection status/health, the incremental cursor + etag, and connector config
  (a feed URL, a Notion database id, etc.).
* :class:`ImportedItem` — the dedup ledger. ``UNIQUE(connection_id,
  source_item_id)`` guarantees a source item is imported **once**; the stored
  ``content_hash`` lets a *changed* item be detected and re-imported while an
  unchanged one is skipped. Each row points at the Kinora ``book`` it became.
* :class:`SyncRun` — append-only history of each sync: counts, status, the error
  (if any), and timing. This is the data behind the connection-health surface.

All three are additive and live alongside the existing schema; nothing references
them from the core ingest/render path.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin, TimestampMixin
from app.db.models.enums import str_enum


class ConnectionStatus(enum.StrEnum):
    """The lifecycle state of an :class:`AppConnection`."""

    #: Authorized and healthy.
    ACTIVE = "active"
    #: Awaiting the OAuth callback (authorize URL handed out, code not yet swapped).
    PENDING = "pending"
    #: The credential expired and could not be refreshed — the user must re-auth.
    NEEDS_REAUTH = "needs_reauth"
    #: Repeated sync failures (not auth) tripped the health threshold.
    ERROR = "error"
    #: The user disconnected it (kept for history; no longer synced).
    DISCONNECTED = "disconnected"


class SyncRunStatus(enum.StrEnum):
    """The terminal (or in-progress) state of one :class:`SyncRun`."""

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"  # some items imported, some failed
    FAILED = "failed"


class AppConnection(StrIdMixin, TimestampMixin, Base):
    """One user's connection to one third-party provider."""

    __tablename__ = "app_connections"
    __table_args__ = (
        # A user keeps at most one *active-ish* connection per provider; the
        # partial-uniqueness is enforced in the service, but this index makes the
        # (user, provider) lookup cheap and is the natural query shape.
        Index("ix_app_connections_user_provider", "user_id", "provider"),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: The connector/registry name ("readwise", "notion", "rss", …).
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    #: A human label shown in the UI (the source account / feed name).
    account_label: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[ConnectionStatus] = mapped_column(
        str_enum(ConnectionStatus, "connection_status"),
        default=ConnectionStatus.PENDING,
        nullable=False,
        index=True,
    )
    #: Sealed token blob (Fernet/v0 — never plaintext). NULL for token-less /
    #: file-upload connectors and while a connection is PENDING.
    sealed_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Granted scopes (space-joined), informational.
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Connector-specific options (feed URL, Notion database id, …).
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    # -- incremental sync state -------------------------------------------- #
    cursor_watermark: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cursor_etag: Mapped[str | None] = mapped_column(String(512), nullable=True)
    cursor_opaque: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- health counters --------------------------------------------------- #
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Consecutive failed syncs (reset to 0 on a clean sync). Drives the ERROR
    #: status threshold and the health surface.
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ImportedItem(StrIdMixin, CreatedAtMixin, Base):
    """The dedup ledger: a source item → the Kinora book it became (once)."""

    __tablename__ = "imported_items"
    __table_args__ = (
        UniqueConstraint("connection_id", "source_item_id", name="uq_imported_items_conn_source"),
        Index("ix_imported_items_book", "book_id"),
    )

    connection_id: Mapped[str] = mapped_column(
        ForeignKey("app_connections.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Stable id within the source (the dedup key).
    source_item_id: Mapped[str] = mapped_column(String(512), nullable=False)
    #: Content fingerprint at import time — a change here means re-import.
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    #: The Kinora book this item became (SET NULL if the book is later deleted).
    book_id: Mapped[str | None] = mapped_column(
        ForeignKey("books.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: When this row was last (re)imported.
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SyncRun(StrIdMixin, CreatedAtMixin, Base):
    """Append-only record of one sync execution for a connection."""

    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_conn_created", "connection_id", "created_at"),)

    connection_id: Mapped[str] = mapped_column(
        ForeignKey("app_connections.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[SyncRunStatus] = mapped_column(
        str_enum(SyncRunStatus, "sync_run_status"),
        default=SyncRunStatus.RUNNING,
        nullable=False,
        index=True,
    )
    #: How the run was kicked off ("manual", "scheduled", "webhook").
    trigger: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    items_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_imported: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "AppConnection",
    "ConnectionStatus",
    "ImportedItem",
    "SyncRun",
    "SyncRunStatus",
]
