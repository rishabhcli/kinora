"""ML dataset + trace pipeline tables: versions, examples, lineage edges

Revision ID: mldata_0001
Revises: 9f3c7a1e2b4d
Create Date: 2026-06-29

Durable, immutable mirror for the ML-data platform (facet A; see
``app/mlplatform/datasets/DESIGN.md``). Three additive tables back the versioned
dataset store. There are **no foreign keys into existing tables** — a committed
dataset version (and the examples it froze) must outlive a book/user deletion,
and ``version_id`` / ``parent_id`` reference the platform's own
content-addressed ids — so this migration is purely ``create_table`` + indexes
and trivially reversible:

* ``mldata_dataset_versions`` — one row per committed, content-addressed dataset
  version (operation + stats snapshot + op-params + tags), indexed on
  ``(name, created_at)`` for the history read;
* ``mldata_examples`` — the frozen training examples of a version (full record as
  JSONB + the hot filter columns role/task/split/content_hash), unique on
  ``(version_id, example_id)``, with loose ``book_id`` / ``session_id``;
* ``mldata_lineage_edges`` — the parent→child edges of the version DAG.

Chained on the llmops head (``9f3c7a1e2b4d``) because the run-trace table the
pipeline ingests through is created there; the seam is read-only.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "mldata_0001"
down_revision: str | None = "9f3c7a1e2b4d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mldata_dataset_versions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("n_examples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("op_params", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mldata_dataset_versions")),
    )
    op.create_index(
        "ix_mldata_versions_name_created",
        "mldata_dataset_versions",
        ["name", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_mldata_versions_content_hash",
        "mldata_dataset_versions",
        ["content_hash"],
        unique=False,
    )
    op.create_index(
        "ix_mldata_versions_operation",
        "mldata_dataset_versions",
        ["operation"],
        unique=False,
    )

    op.create_table(
        "mldata_examples",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("example_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("task", sa.String(length=16), nullable=False),
        sa.Column("split", sa.String(length=16), nullable=False, server_default="unassigned"),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("reward", sa.Float(), nullable=True),
        sa.Column("scrubbed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("record", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mldata_examples")),
        sa.UniqueConstraint(
            "version_id", "example_id", name="uq_mldata_examples_version_example"
        ),
    )
    op.create_index("ix_mldata_examples_version", "mldata_examples", ["version_id"], unique=False)
    op.create_index(
        "ix_mldata_examples_role_task", "mldata_examples", ["role", "task"], unique=False
    )
    op.create_index(
        "ix_mldata_examples_split", "mldata_examples", ["version_id", "split"], unique=False
    )
    op.create_index(
        "ix_mldata_examples_content_hash", "mldata_examples", ["content_hash"], unique=False
    )
    op.create_index("ix_mldata_examples_book", "mldata_examples", ["book_id"], unique=False)

    op.create_table(
        "mldata_lineage_edges",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("parent_id", sa.String(length=64), nullable=False),
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mldata_lineage_edges")),
        sa.UniqueConstraint(
            "parent_id", "version_id", name="uq_mldata_lineage_parent_child"
        ),
    )
    op.create_index(
        "ix_mldata_lineage_child", "mldata_lineage_edges", ["version_id"], unique=False
    )
    op.create_index(
        "ix_mldata_lineage_parent", "mldata_lineage_edges", ["parent_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_mldata_lineage_parent", table_name="mldata_lineage_edges")
    op.drop_index("ix_mldata_lineage_child", table_name="mldata_lineage_edges")
    op.drop_table("mldata_lineage_edges")

    op.drop_index("ix_mldata_examples_book", table_name="mldata_examples")
    op.drop_index("ix_mldata_examples_content_hash", table_name="mldata_examples")
    op.drop_index("ix_mldata_examples_split", table_name="mldata_examples")
    op.drop_index("ix_mldata_examples_role_task", table_name="mldata_examples")
    op.drop_index("ix_mldata_examples_version", table_name="mldata_examples")
    op.drop_table("mldata_examples")

    op.drop_index("ix_mldata_versions_operation", table_name="mldata_dataset_versions")
    op.drop_index("ix_mldata_versions_content_hash", table_name="mldata_dataset_versions")
    op.drop_index("ix_mldata_versions_name_created", table_name="mldata_dataset_versions")
    op.drop_table("mldata_dataset_versions")
