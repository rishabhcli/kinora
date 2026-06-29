"""ML-data platform persistence: dataset versions, examples, lineage edges.

The pure in-memory components (:mod:`app.mlplatform.datasets.versioning`) are the
source of truth at runtime; these tables are the **durable, immutable mirror** so
a committed dataset version (and the lineage DAG that produced it) survives a
restart and can be served to the sibling alignment / serving facets without
replaying the whole pipeline.

Three tables back :mod:`app.mlplatform.datasets`:

* ``mldata_dataset_versions`` — one row per committed
  :class:`~app.mlplatform.datasets.versioning.DatasetVersion`: the
  content-addressed ``version_id``, the dataset ``name`` (the moving head), the
  ``operation`` that produced it, a JSONB stats snapshot, op-params, tags, and a
  ``content_hash``. ``version_id`` is the PK; ``(name, created_at)`` is indexed
  for the history read. Immutable: a row is inserted once and never updated.
* ``mldata_examples`` — the frozen :class:`~app.mlplatform.datasets.contracts.TraceExample`
  rows belonging to a version (the full record as JSONB + the hot columns the
  facets filter on: role / task / split / content_hash). ``(version_id, example_id)``
  is UNIQUE. Loose ``book_id`` / ``session_id`` (no FK — exactly like the
  llmops run-trace + analytics-event attribution; the dataset must outlive a
  book/user deletion).
* ``mldata_lineage_edges`` — the parent→child edges of the version DAG
  (``parent_id`` → ``version_id``), so the lineage walk is a cheap recursive read
  rather than a JSON unpack.

All additive: **no FK references INTO existing tables**, so the migration
(``mldata_0001``) is pure ``create_table`` + indexes and trivially reversible.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, StrIdMixin


class MLDataDatasetVersion(StrIdMixin, Base):
    """One committed, immutable dataset version (content-addressed)."""

    __tablename__ = "mldata_dataset_versions"
    __table_args__ = (
        Index("ix_mldata_versions_name_created", "name", "created_at"),
        Index("ix_mldata_versions_content_hash", "content_hash"),
        Index("ix_mldata_versions_operation", "operation"),
    )

    #: ``id`` (from StrIdMixin) IS the content-addressed ``version_id``.
    #: The dataset name this version is a snapshot of (the moving head).
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The producing op: ingest / scrub / dedup / split / label / filter / merge / import.
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    n_examples: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The :class:`DatasetStats` snapshot (JSONB).
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: The op-params that produced this version (stage report JSON).
    op_params: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MLDataExample(StrIdMixin, Base):
    """One frozen training example belonging to a dataset version."""

    __tablename__ = "mldata_examples"
    __table_args__ = (
        UniqueConstraint("version_id", "example_id", name="uq_mldata_examples_version_example"),
        Index("ix_mldata_examples_version", "version_id"),
        Index("ix_mldata_examples_role_task", "role", "task"),
        Index("ix_mldata_examples_split", "version_id", "split"),
        Index("ix_mldata_examples_content_hash", "content_hash"),
        Index("ix_mldata_examples_book", "book_id"),
    )

    #: The owning version's content-addressed id (no FK; immutable mirror).
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The example's stable id (``ex_…``); unique within a version.
    example_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    task: Mapped[str] = mapped_column(String(16), nullable=False)
    split: Mapped[str] = mapped_column(String(16), nullable=False, default="unassigned")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reward: Mapped[float | None] = mapped_column(Float, nullable=True)
    scrubbed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: The full :meth:`TraceExample.to_record` payload (JSONB).
    record: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: Loose attribution (no FK; same pattern as the llmops run trace).
    book_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MLDataLineageEdge(StrIdMixin, Base):
    """A parent→child edge of the dataset-version DAG."""

    __tablename__ = "mldata_lineage_edges"
    __table_args__ = (
        UniqueConstraint("parent_id", "version_id", name="uq_mldata_lineage_parent_child"),
        Index("ix_mldata_lineage_child", "version_id"),
        Index("ix_mldata_lineage_parent", "parent_id"),
    )

    parent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)


__all__ = [
    "MLDataDatasetVersion",
    "MLDataExample",
    "MLDataLineageEdge",
]
