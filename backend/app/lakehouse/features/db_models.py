"""Durable persistence for the feature store (additive ``feature_store_*`` tables).

The pure feature platform (registry + stores) is infra-free; these tables give it
a durable spine so an offline history, a registry snapshot, and a materialisation
log survive process restarts and are shared across the API + worker processes:

* ``feature_store_offline_rows`` — the durable offline feature history. One row per
  observation: the feature view name, a stable entity-key string, the event +
  arrival timestamps the point-in-time join sorts on, and the feature payload as
  JSONB (schemaless so a new feature never needs a migration, mirroring the
  ``book_interactions.kind``-as-string decision in the recommendations warehouse).
* ``feature_store_view_defs`` — a content-addressed snapshot of every registered
  feature view (name + version + the JSON definition), so the registry can be
  rehydrated and a version's definition audited.
* ``feature_store_materializations`` — the materialisation run log (view, version,
  as-of, rows written, coverage) feeding freshness + lineage monitoring.

All three are **additive** and key only on their own ids; they carry no foreign
keys into existing tables (an entity key is an opaque string, not an FK, because a
feature view may be keyed on a non-``users``/``books`` entity). Registered on
``Base.metadata`` via the additive import in ``app/db/models/__init__.py``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin, TimestampMixin


class FeatureOfflineRow(StrIdMixin, CreatedAtMixin, Base):
    """One durable offline feature observation (the point-in-time join's source)."""

    __tablename__ = "feature_store_offline_rows"
    __table_args__ = (
        # The hot read path: all rows for a (view, entity) ordered by event time.
        Index(
            "ix_feature_store_offline_view_key_event",
            "view_name",
            "entity_key",
            "event_timestamp",
        ),
        # Idempotent append guard (one observation per view/key/event/arrival).
        Index(
            "uq_feature_store_offline_identity",
            "view_name",
            "entity_key",
            "event_timestamp",
            "created_timestamp",
            unique=True,
        ),
    )

    #: The feature view this observation belongs to.
    view_name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: A stable string encoding of the entity key tuple (join-key values joined).
    entity_key: Mapped[str] = mapped_column(String(512), nullable=False)
    #: The join-key columns→values for this row (rehydrates ``FeatureRow.keys``).
    keys: Mapped[dict] = mapped_column(JSONB, nullable=False)
    #: The feature payload (feature name → value); schemaless JSONB.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    #: Event time the point-in-time join merges on.
    event_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Arrival/ingestion time — the late-data tie-breaker at equal event time.
    created_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class FeatureViewDef(StrIdMixin, TimestampMixin, Base):
    """A content-addressed snapshot of a registered feature view definition."""

    __tablename__ = "feature_store_view_defs"
    __table_args__ = (
        Index(
            "uq_feature_store_view_defs_name_version",
            "view_name",
            "version",
            unique=True,
        ),
    )

    view_name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: The content-addressed version assigned by the registry.
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The serialised view definition (entities, features, ttl, source, transform).
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)


class FeatureMaterialization(StrIdMixin, CreatedAtMixin, Base):
    """One materialisation run (offline→online) for freshness/lineage telemetry."""

    __tablename__ = "feature_store_materializations"
    __table_args__ = (
        Index("ix_feature_store_materializations_view_asof", "view_name", "as_of"),
    )

    view_name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The reference instant the latest-value pick was taken at.
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rows_written: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    keys_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Fraction of distinct entity keys that had a non-stale value (0..1).
    coverage: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


__all__ = ["FeatureMaterialization", "FeatureOfflineRow", "FeatureViewDef"]
