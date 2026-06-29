"""SQLAlchemy ORM rows for the plugin/extension platform.

Five tables, all on the shared :class:`~app.db.base.Base` so Alembic and
``create_all`` see them. The platform follows the project convention of storing
the rich, validated object (the serialized :class:`~app.platform.plugins.manifest.PluginManifest`,
the version ledger, etc.) in a JSONB column while mirroring the *hot* query
fields (id, version, state, risk) into typed columns for cheap filtering.

* ``plugin_registry`` — the **marketplace catalog**: one row per published
  ``(plugin_id, version)`` artifact. Holds the manifest JSONB, the plugin source
  text, the content ``digest`` (UNIQUE, idempotent publish), the detached
  signature, the review ``status``, and aggregate rating stats. ``yanked`` marks
  a withdrawn version that stays resolvable for installed users but is hidden
  from new installs.
* ``plugin_installation`` — one row per tenant-installed plugin (the durable
  :class:`~app.platform.plugins.lifecycle.PluginInstallation`): its current
  ``version``, lifecycle ``state``, failure counter, granted-capability set, and
  the JSONB version ``history`` ledger for rollback.
* ``plugin_review`` — moderation decisions on a registry artifact (approve /
  reject / request-changes) with the reviewer + notes; append-only.
* ``plugin_rating`` — one rating (1–5 + optional review text) per
  (plugin_id, user); UNIQUE makes a re-rating an upsert, not a duplicate.
* ``plugin_audit`` — an append-only log of every lifecycle/registry mutation.

Like the flags ORM, status/state/risk are stored as plain strings (the pure
enums in :mod:`app.platform.plugins` own validation in/out) so this module has
no dependency on ``db.models.enums`` and stays additive.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Boolean,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin, TimestampMixin


class PluginRegistryEntry(StrIdMixin, TimestampMixin, Base):
    """A published plugin artifact ``(plugin_id, version)`` in the marketplace."""

    __tablename__ = "plugin_registry"
    __table_args__ = (
        UniqueConstraint("plugin_id", "version", name="uq_plugin_registry_plugin_id_version"),
        UniqueConstraint("digest", name="uq_plugin_registry_digest"),
        Index("ix_plugin_registry_status_risk", "status", "max_risk"),
        Index("ix_plugin_registry_plugin", "plugin_id", "yanked"),
    )

    plugin_id: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    publisher: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    max_risk: Mapped[str] = mapped_column(String(16), default="low", nullable=False)
    yanked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    signed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    rating_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rating_sum: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    install_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class PluginInstallationRow(StrIdMixin, TimestampMixin, Base):
    """A tenant-installed plugin and its lifecycle state."""

    __tablename__ = "plugin_installation"
    __table_args__ = (
        UniqueConstraint("owner", "plugin_id", name="uq_plugin_installation_owner_plugin_id"),
        Index("ix_plugin_installation_owner_state", "owner", "state"),
    )

    owner: Mapped[str] = mapped_column(String(160), nullable=False)
    plugin_id: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="installed", nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    granted: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)


class PluginReviewRow(StrIdMixin, CreatedAtMixin, Base):
    """A moderation decision on a registry artifact (append-only)."""

    __tablename__ = "plugin_review"
    __table_args__ = (Index("ix_plugin_review_artifact", "plugin_id", "version", "created_at"),)

    plugin_id: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewer: Mapped[str | None] = mapped_column(String(160), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)


class PluginRatingRow(StrIdMixin, TimestampMixin, Base):
    """A single user's rating of a plugin (UNIQUE per (plugin_id, user))."""

    __tablename__ = "plugin_rating"
    __table_args__ = (
        UniqueConstraint("plugin_id", "user_id", name="uq_plugin_rating_plugin_id_user_id"),
        Index("ix_plugin_rating_plugin", "plugin_id"),
    )

    plugin_id: Mapped[str] = mapped_column(String(160), nullable=False)
    user_id: Mapped[str] = mapped_column(String(160), nullable=False)
    stars: Mapped[int] = mapped_column(Integer, nullable=False)
    review: Mapped[str] = mapped_column(Text, default="", nullable=False)


class PluginAuditRow(StrIdMixin, CreatedAtMixin, Base):
    """An append-only audit record for a plugin lifecycle/registry mutation."""

    __tablename__ = "plugin_audit"
    __table_args__ = (Index("ix_plugin_audit_subject", "plugin_id", "created_at"),)

    plugin_id: Mapped[str] = mapped_column(String(160), nullable=False)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(160), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


__all__ = [
    "PluginAuditRow",
    "PluginInstallationRow",
    "PluginRatingRow",
    "PluginRegistryEntry",
    "PluginReviewRow",
]
