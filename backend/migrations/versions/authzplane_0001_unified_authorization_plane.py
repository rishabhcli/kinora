"""Unified authorization plane — relation tuples + decision log (kinora.md §5 §8.3)

Revision ID: authzplane_0001
Revises: a1b2c3d4e5f6
Create Date: 2026-06-29

Additive migration for the unified authorization plane (``app.platform.authz``).
It creates two new tables and touches no existing schema:

* ``authz_relation_tuples`` — the Google-Zanzibar relationship facts
  (``object#relation@subject``) that back the native relationship engine: the
  durable form of the in-memory tuple store. Four indexes mirror the in-memory
  store's access paths — forward (object+relation) for ``check``, reverse
  (subject+relation+object_type) for the ``list_objects`` reverse index, incoming
  (subject) for tuple-to-userset back-walks, and a full-tuple unique index that
  makes (re)writes idempotent.
* ``authz_decision_log`` — the append-only audit of every ``check`` the plane
  resolves (subject / action / resource / effect / reasons / digest). This is the
  unified "who was allowed/denied what, and why" the scattered legacy checks
  never had.

Everything else in the plane (the policy DSL + evaluator, RBAC/ABAC, the
combining algorithms, the decision cache, the policy testing/coverage/simulation,
and the adapters that fold the existing checks in) is pure code; this migration
only lays the two durable tables those services persist to. The plane reads the
personal-owner / workspace / RBAC facts through adapters over the *existing*
tables, so this store never competes with the legacy schema as a source of truth.

Branches off the shared base ``a1b2c3d4e5f6`` as its own head (one head per
parallel platform facet), with the domain-unique id ``authzplane_0001``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "authzplane_0001"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- authz_relation_tuples ---------------------------------------------- #
    op.create_table(
        "authz_relation_tuples",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("object_type", sa.String(length=64), nullable=False),
        sa.Column("object_id", sa.String(length=128), nullable=False),
        sa.Column("relation", sa.String(length=64), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("subject_relation", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_authz_relation_tuples")),
    )
    op.create_index(
        "ix_authz_tuples_forward",
        "authz_relation_tuples",
        ["object_type", "object_id", "relation"],
        unique=False,
    )
    op.create_index(
        "ix_authz_tuples_reverse",
        "authz_relation_tuples",
        ["subject_type", "subject_id", "relation", "object_type"],
        unique=False,
    )
    op.create_index(
        "ix_authz_tuples_incoming",
        "authz_relation_tuples",
        ["subject_type", "subject_id"],
        unique=False,
    )
    op.create_index(
        "uq_authz_tuples_full",
        "authz_relation_tuples",
        [
            "object_type",
            "object_id",
            "relation",
            "subject_type",
            "subject_id",
            "subject_relation",
        ],
        unique=True,
    )

    # --- authz_decision_log -------------------------------------------------- #
    op.create_table(
        "authz_decision_log",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("subject_ref", sa.String(length=256), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("resource_ref", sa.String(length=256), nullable=False),
        sa.Column("effect", sa.String(length=16), nullable=False),
        sa.Column("reasons", sa.Text(), nullable=False),
        sa.Column("cached", sa.Boolean(), nullable=False),
        sa.Column("digest", sa.String(length=32), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_authz_decision_log")),
    )
    op.create_index(
        op.f("ix_authz_decision_log_subject_ref"),
        "authz_decision_log",
        ["subject_ref"],
        unique=False,
    )
    op.create_index(
        op.f("ix_authz_decision_log_action"),
        "authz_decision_log",
        ["action"],
        unique=False,
    )
    op.create_index(
        op.f("ix_authz_decision_log_resource_ref"),
        "authz_decision_log",
        ["resource_ref"],
        unique=False,
    )
    op.create_index(
        op.f("ix_authz_decision_log_digest"),
        "authz_decision_log",
        ["digest"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_authz_decision_log_digest"), table_name="authz_decision_log")
    op.drop_index(
        op.f("ix_authz_decision_log_resource_ref"), table_name="authz_decision_log"
    )
    op.drop_index(op.f("ix_authz_decision_log_action"), table_name="authz_decision_log")
    op.drop_index(
        op.f("ix_authz_decision_log_subject_ref"), table_name="authz_decision_log"
    )
    op.drop_table("authz_decision_log")

    op.drop_index("uq_authz_tuples_full", table_name="authz_relation_tuples")
    op.drop_index("ix_authz_tuples_incoming", table_name="authz_relation_tuples")
    op.drop_index("ix_authz_tuples_reverse", table_name="authz_relation_tuples")
    op.drop_index("ix_authz_tuples_forward", table_name="authz_relation_tuples")
    op.drop_table("authz_relation_tuples")
