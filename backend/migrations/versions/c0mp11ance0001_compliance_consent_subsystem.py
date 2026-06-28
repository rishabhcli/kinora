"""Compliance & consent subsystem: consent, retention, holds, DSAR, ledger

Revision ID: c0mp11ance0001
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28

Additive migration creating the seven compliance tables (kinora.md §8 audit-chain
design, §11 accountability). Touches no existing table. These complement the
``dataportability`` domain's GDPR export/erasure with the governance layer:
versioned consent + proof records, per-data-class retention, legal holds, the
DSAR workflow, and one consolidated hash-chained compliance audit ledger.

All enums are stored as portable VARCHAR + named CHECK constraints (native_enum
False), matching the rest of the schema. Subject foreign keys to ``users.id`` use
ON DELETE SET NULL so the consent proof trail / DSAR / ledger survive account
erasure (GDPR Art. 7(1) demonstrability + append-only accountability).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c0mp11ance0001"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Enum value vocabularies (kept inline so the migration is self-describing and
# does not import app code, which is the Alembic convention here).
_PURPOSE = (
    "adaptation",
    "personalization",
    "analytics",
    "model_training",
    "transactional_email",
    "marketing_email",
)
_BASIS = (
    "consent",
    "contract",
    "legal_obligation",
    "vital_interests",
    "public_task",
    "legitimate_interests",
)
_DATA_CLASS = (
    "account",
    "uploaded_book",
    "generated_media",
    "reading_session",
    "directing_preference",
    "audit_log",
    "billing_record",
)
_CONSENT_ACTION = ("grant", "withdraw")
_POLICY_STATUS = ("draft", "active", "superseded")
_HOLD_STATUS = ("active", "lifted")
_DSAR_KIND = (
    "access",
    "erasure",
    "rectification",
    "portability",
    "restriction",
    "objection",
)
_DSAR_STATE = (
    "received",
    "verifying",
    "in_progress",
    "extended",
    "completed",
    "rejected",
    "cancelled",
)
_LEDGER_CATEGORY = (
    "consent",
    "retention",
    "dsar",
    "legal_hold",
    "policy",
    "security",
    "moderation",
    "billing",
)


def _enum(values: tuple[str, ...], name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, validate_strings=True)


def upgrade() -> None:
    # ----- consent_policies --------------------------------------------------- #
    op.create_table(
        "consent_policies",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("purpose", _enum(_PURPOSE, "consent_policy_purpose"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", _enum(_POLICY_STATUS, "consent_policy_status"), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_hash", sa.String(length=64), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_consent_policies"),
        sa.UniqueConstraint("purpose", "version", name="uq_consent_policies_purpose_version"),
    )
    op.create_index("ix_consent_policies_purpose_status", "consent_policies", ["purpose", "status"])

    # ----- consent_records ---------------------------------------------------- #
    op.create_table(
        "consent_records",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=True),
        sa.Column("purpose", _enum(_PURPOSE, "consent_record_purpose"), nullable=False),
        sa.Column("action", _enum(_CONSENT_ACTION, "consent_record_action"), nullable=False),
        sa.Column("policy_id", sa.String(length=64), nullable=True),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column("lawful_basis", _enum(_BASIS, "consent_record_basis"), nullable=False),
        sa.Column("source", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_consent_records"),
        sa.UniqueConstraint("seq", name="uq_consent_records_seq"),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["users.id"],
            name="fk_consent_records_subject_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["consent_policies.id"],
            name="fk_consent_records_policy_id_consent_policies",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_consent_records_subject_purpose",
        "consent_records",
        ["subject_id", "purpose", "seq"],
    )
    op.create_index("ix_consent_records_policy", "consent_records", ["policy_id"])

    # ----- retention_rules ---------------------------------------------------- #
    op.create_table(
        "retention_rules",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("data_class", _enum(_DATA_CLASS, "retention_rule_data_class"), nullable=False),
        sa.Column("ttl_days", sa.Integer(), nullable=True),
        sa.Column("lawful_basis", _enum(_BASIS, "retention_rule_basis"), nullable=False),
        sa.Column("expire_on_consent_withdrawal", sa.Boolean(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_retention_rules"),
        sa.UniqueConstraint("data_class", name="uq_retention_rules_data_class"),
    )

    # ----- legal_holds -------------------------------------------------------- #
    op.create_table(
        "legal_holds",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=True),
        sa.Column("data_class", _enum(_DATA_CLASS, "legal_hold_data_class"), nullable=True),
        sa.Column("status", _enum(_HOLD_STATUS, "legal_hold_status"), nullable=False),
        sa.Column("matter_id", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("placed_by", sa.String(length=128), nullable=False),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lifted_by", sa.String(length=128), nullable=True),
        sa.Column("lifted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_legal_holds"),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["users.id"],
            name="fk_legal_holds_subject_id_users",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_legal_holds_subject_status", "legal_holds", ["subject_id", "status"])
    op.create_index("ix_legal_holds_matter", "legal_holds", ["matter_id"])

    # ----- dsar_requests ------------------------------------------------------ #
    op.create_table(
        "dsar_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=True),
        sa.Column("subject_email", sa.String(length=320), nullable=True),
        sa.Column("kind", _enum(_DSAR_KIND, "dsar_request_kind"), nullable=False),
        sa.Column("state", _enum(_DSAR_STATE, "dsar_request_state"), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extended_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_dsar_requests"),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["users.id"],
            name="fk_dsar_requests_subject_id_users",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_dsar_requests_subject_state", "dsar_requests", ["subject_id", "state"])
    op.create_index("ix_dsar_requests_due", "dsar_requests", ["state", "due_at"])

    # ----- dsar_events -------------------------------------------------------- #
    op.create_table(
        "dsar_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("from_state", _enum(_DSAR_STATE, "dsar_event_from_state"), nullable=True),
        sa.Column("to_state", _enum(_DSAR_STATE, "dsar_event_to_state"), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_dsar_events"),
        sa.UniqueConstraint("seq", name="uq_dsar_events_seq"),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["dsar_requests.id"],
            name="fk_dsar_events_request_id_dsar_requests",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_dsar_events_request", "dsar_events", ["request_id", "seq"])

    # ----- compliance_ledger -------------------------------------------------- #
    op.create_table(
        "compliance_ledger",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column(
            "category", _enum(_LEDGER_CATEGORY, "compliance_ledger_category"), nullable=False
        ),
        sa.Column("event", sa.String(length=128), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=True),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("prev_hash", sa.String(length=64), nullable=True),
        sa.Column("entry_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_compliance_ledger"),
        sa.UniqueConstraint("seq", name="uq_compliance_ledger_seq"),
    )
    op.create_index(
        "ix_compliance_ledger_category", "compliance_ledger", ["category", "created_at"]
    )
    op.create_index(
        "ix_compliance_ledger_subject", "compliance_ledger", ["subject_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_compliance_ledger_subject", table_name="compliance_ledger")
    op.drop_index("ix_compliance_ledger_category", table_name="compliance_ledger")
    op.drop_table("compliance_ledger")

    op.drop_index("ix_dsar_events_request", table_name="dsar_events")
    op.drop_table("dsar_events")

    op.drop_index("ix_dsar_requests_due", table_name="dsar_requests")
    op.drop_index("ix_dsar_requests_subject_state", table_name="dsar_requests")
    op.drop_table("dsar_requests")

    op.drop_index("ix_legal_holds_matter", table_name="legal_holds")
    op.drop_index("ix_legal_holds_subject_status", table_name="legal_holds")
    op.drop_table("legal_holds")

    op.drop_table("retention_rules")

    op.drop_index("ix_consent_records_policy", table_name="consent_records")
    op.drop_index("ix_consent_records_subject_purpose", table_name="consent_records")
    op.drop_table("consent_records")

    op.drop_index("ix_consent_policies_purpose_status", table_name="consent_policies")
    op.drop_table("consent_policies")
