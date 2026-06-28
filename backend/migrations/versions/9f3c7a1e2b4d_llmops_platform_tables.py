"""LLM-ops platform tables: prompt registry, changelog, run traces, eval reports

Revision ID: 9f3c7a1e2b4d
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28

Durable backing for the :mod:`app.llmops` platform (see
``app/llmops/DESIGN.md``). Four additive tables, no foreign keys into existing
tables (``book_id`` / ``session_id`` on runs are loose attribution like the cost
meter), so this is purely ``create_table`` + indexes and trivially reversible:

* ``llmops_prompt_versions`` — registered prompt versions (semver + content
  address + lifecycle status), unique on ``(prompt_key, version)``;
* ``llmops_changelog`` — append-only registry-mutation audit;
* ``llmops_runs`` — structured run traces (prompt + inputs + outputs + tokens +
  cost + latency + guardrail decision);
* ``llmops_eval_reports`` — cached eval / A-B / regression reports (JSONB).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9f3c7a1e2b4d"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llmops_prompt_versions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("prompt_key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("prompt_tag", sa.String(length=64), nullable=False),
        sa.Column("system", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llmops_prompt_versions")),
        sa.UniqueConstraint("prompt_key", "version", name="uq_llmops_prompt_key_version"),
    )
    op.create_index(
        op.f("ix_llmops_prompt_versions_sha256"),
        "llmops_prompt_versions",
        ["sha256"],
        unique=False,
    )
    op.create_index(
        "ix_llmops_prompt_key_status",
        "llmops_prompt_versions",
        ["prompt_key", "status"],
        unique=False,
    )

    op.create_table(
        "llmops_changelog",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("prompt_key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("author", sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llmops_changelog")),
    )
    op.create_index(
        "ix_llmops_changelog_key",
        "llmops_changelog",
        ["prompt_key", "created_at"],
        unique=False,
    )

    op.create_table(
        "llmops_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("prompt_key", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("guardrail_decision", sa.String(length=16), nullable=True),
        sa.Column("cache_hit", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("book_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llmops_runs")),
    )
    op.create_index(
        "ix_llmops_runs_key_version",
        "llmops_runs",
        ["prompt_key", "prompt_version"],
        unique=False,
    )
    op.create_index("ix_llmops_runs_book", "llmops_runs", ["book_id"], unique=False)
    op.create_index("ix_llmops_runs_session", "llmops_runs", ["session_id"], unique=False)
    op.create_index(
        "ix_llmops_runs_model_created",
        "llmops_runs",
        ["model", "created_at"],
        unique=False,
    )

    op.create_table(
        "llmops_eval_reports",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("prompt_key", sa.String(length=64), nullable=False),
        sa.Column("dataset_name", sa.String(length=128), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llmops_eval_reports")),
    )
    op.create_index(
        "ix_llmops_eval_kind_key",
        "llmops_eval_reports",
        ["kind", "prompt_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_llmops_eval_kind_key", table_name="llmops_eval_reports")
    op.drop_table("llmops_eval_reports")

    op.drop_index("ix_llmops_runs_model_created", table_name="llmops_runs")
    op.drop_index("ix_llmops_runs_session", table_name="llmops_runs")
    op.drop_index("ix_llmops_runs_book", table_name="llmops_runs")
    op.drop_index("ix_llmops_runs_key_version", table_name="llmops_runs")
    op.drop_table("llmops_runs")

    op.drop_index("ix_llmops_changelog_key", table_name="llmops_changelog")
    op.drop_table("llmops_changelog")

    op.drop_index("ix_llmops_prompt_key_status", table_name="llmops_prompt_versions")
    op.drop_index(op.f("ix_llmops_prompt_versions_sha256"), table_name="llmops_prompt_versions")
    op.drop_table("llmops_prompt_versions")
