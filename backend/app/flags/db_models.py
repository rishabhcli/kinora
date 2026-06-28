"""SQLAlchemy ORM rows for the feature-flags platform.

Four tables, all on the shared :class:`~app.db.base.Base` so Alembic and
``create_all`` see them:

* ``feature_flags`` — the durable flag registry. The evaluable definition lives
  in a JSONB ``definition`` column (the serialized :class:`~app.flags.models.Flag`);
  ``key`` is the natural id, ``version`` bumps on every write, and the hot
  evaluation fields (``enabled``/``archived``) are mirrored to columns for cheap
  filtering without parsing JSON.
* ``flag_experiments`` — experiment definitions (serialized
  :class:`~app.flags.experiment.Experiment` in JSONB), keyed by ``key``.
* ``flag_exposures`` — one row per (experiment, unit, version) exposure, with a
  UNIQUE constraint that makes logging idempotent (the engine's ``exposure_key``
  maps to ``dedup_key``).
* ``flag_audit`` — an append-only change log: action, actor, the before/after
  snapshots, and the computed diff.

The ORM intentionally stores status/kind as plain strings (the pure
:class:`~app.flags.models` enums own validation on the way in/out) so this module
has no dependency on the shared ``db.models.enums`` and stays additive.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin, TimestampMixin


class FeatureFlag(StrIdMixin, TimestampMixin, Base):
    """A durable feature flag; the evaluable form lives in ``definition`` (JSONB)."""

    __tablename__ = "feature_flags"
    __table_args__ = (
        UniqueConstraint("key", name="uq_feature_flags_key"),
        Index("ix_feature_flags_enabled_archived", "enabled", "archived"),
    )

    key: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    name: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)


class FlagExperiment(StrIdMixin, TimestampMixin, Base):
    """A durable experiment; the definition lives in ``definition`` (JSONB)."""

    __tablename__ = "flag_experiments"
    __table_args__ = (
        UniqueConstraint("key", name="uq_flag_experiments_key"),
        Index("ix_flag_experiments_status", "status"),
    )

    key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    name: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)


class FlagExposure(StrIdMixin, CreatedAtMixin, Base):
    """One recorded exposure; UNIQUE(dedup_key) makes logging idempotent."""

    __tablename__ = "flag_exposures"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_flag_exposures_dedup_key"),
        Index("ix_flag_exposures_experiment_variant", "experiment_key", "variant_key"),
    )

    experiment_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    experiment_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    variant_key: Mapped[str] = mapped_column(String(128), nullable=False)
    unit_key: Mapped[str] = mapped_column(String(256), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(512), nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class FlagAudit(StrIdMixin, CreatedAtMixin, Base):
    """An append-only audit record for a flag/experiment mutation."""

    __tablename__ = "flag_audit"
    __table_args__ = (
        Index("ix_flag_audit_subject", "subject_kind", "subject_key", "created_at"),
    )

    subject_kind: Mapped[str] = mapped_column(String(32), nullable=False)  # flag | experiment
    subject_key: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(256), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)


__all__ = ["FeatureFlag", "FlagAudit", "FlagExperiment", "FlagExposure"]
