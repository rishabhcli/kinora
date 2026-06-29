"""Zero-trust data-at-rest crypto plane: KEK registry, token vault, blind index, rotation

Revision ID: tokvault_0001
Revises: f7a2b9c4d1e8
Create Date: 2026-06-29

Additive migration creating the five ``crypto_*`` tables for the zero-trust
data-at-rest facet (application-layer encryption — kinora.md §8 memory/audit,
§11 accountability). Touches no existing table; chains off the auth security
plane (``f7a2b9c4d1e8``) because PII protection is the same trust boundary.

Tables:

* ``crypto_kek_registry``      — catalogue of KEK (id, version) lifecycle states
  (the queryable catalogue; key *material* stays inside the KMS / facet A).
* ``crypto_token_vault``       — tokenization vault: token → AEAD-encrypted PII +
  authorisation policy. Unique token; keyed plaintext id for deterministic dedup.
* ``crypto_token_access_log``  — append-only audit of detokenize attempts.
* ``crypto_blind_index``       — companion keyed-HMAC search tokens for searchable
  encrypted columns (equality / prefix / range).
* ``crypto_rotation_job``      — durable, resumable cursor + counters for online
  KEK re-wrap / DEK re-encryption passes.

All enums are stored as portable VARCHAR + named CHECK constraints
(``native_enum=False``), matching the rest of the schema. The migration imports
no app code (Alembic convention here), so the enum vocabularies are inlined.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "tokvault_0001"
down_revision: str | None = "f7a2b9c4d1e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Enum value vocabularies (kept inline so the migration is self-describing).
_KEK_LIFECYCLE = ("enabled", "disabled", "pending_deletion", "destroyed")
_TOKEN_SCHEME = ("random", "deterministic")
_ROTATION_MODE = ("rewrap", "reencrypt")
_ROTATION_STATUS = ("pending", "running", "paused", "completed", "failed")
_BLIND_INDEX_KIND = ("equality", "prefix", "range")


def upgrade() -> None:
    # -- crypto_kek_registry ------------------------------------------------- #
    op.create_table(
        "crypto_kek_registry",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("kek_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(*_KEK_LIFECYCLE, name="ck_crypto_kek_registry_state", native_enum=False),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_crypto_kek_registry"),
        sa.UniqueConstraint("kek_id", "version", name="uq_crypto_kek_registry_kek_id_version"),
    )
    op.create_index(
        "ix_crypto_kek_registry_kek_id_state", "crypto_kek_registry", ["kek_id", "state"]
    )

    # -- crypto_token_vault -------------------------------------------------- #
    op.create_table(
        "crypto_token_vault",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("token", sa.String(length=256), nullable=False),
        sa.Column("plaintext_id", sa.String(length=64), nullable=True),
        sa.Column("envelope", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_dek", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "scheme",
            sa.Enum(*_TOKEN_SCHEME, name="ck_crypto_token_vault_scheme", native_enum=False),
            nullable=False,
        ),
        sa.Column("allowed_purposes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("data_class", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_crypto_token_vault"),
        sa.UniqueConstraint("token", name="uq_crypto_token_vault_token"),
    )
    op.create_index(
        "ix_crypto_token_vault_plaintext_id", "crypto_token_vault", ["plaintext_id"]
    )
    op.create_index("ix_crypto_token_vault_data_class", "crypto_token_vault", ["data_class"])

    # -- crypto_token_access_log -------------------------------------------- #
    op.create_table(
        "crypto_token_access_log",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("token", sa.String(length=256), nullable=False),
        sa.Column("actor", sa.String(length=256), nullable=False),
        sa.Column("purpose", sa.String(length=128), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("seq", name="pk_crypto_token_access_log"),
    )
    op.create_index("ix_crypto_token_access_log_token", "crypto_token_access_log", ["token"])
    op.create_index("ix_crypto_token_access_log_actor", "crypto_token_access_log", ["actor"])

    # -- crypto_blind_index -------------------------------------------------- #
    op.create_table(
        "crypto_blind_index",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("source_table", sa.String(length=128), nullable=False),
        sa.Column("source_column", sa.String(length=128), nullable=False),
        sa.Column("source_row_id", sa.String(length=64), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(*_BLIND_INDEX_KIND, name="ck_crypto_blind_index_kind", native_enum=False),
            nullable=False,
        ),
        sa.Column("token", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("seq", name="pk_crypto_blind_index"),
    )
    op.create_index(
        "ix_crypto_blind_index_lookup",
        "crypto_blind_index",
        ["source_table", "source_column", "kind", "token"],
    )
    op.create_index(
        "ix_crypto_blind_index_row", "crypto_blind_index", ["source_table", "source_row_id"]
    )

    # -- crypto_rotation_job ------------------------------------------------- #
    op.create_table(
        "crypto_rotation_job",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "mode",
            sa.Enum(*_ROTATION_MODE, name="ck_crypto_rotation_job_mode", native_enum=False),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(*_ROTATION_STATUS, name="ck_crypto_rotation_job_status", native_enum=False),
            nullable=False,
        ),
        sa.Column("source_table", sa.String(length=128), nullable=False),
        sa.Column("source_column", sa.String(length=128), nullable=False),
        sa.Column("kek_id", sa.String(length=128), nullable=False),
        sa.Column("target_version", sa.Integer(), nullable=True),
        sa.Column("cursor", sa.String(length=256), nullable=True),
        sa.Column("scanned", sa.BigInteger(), nullable=False),
        sa.Column("rotated", sa.BigInteger(), nullable=False),
        sa.Column("skipped", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_crypto_rotation_job"),
    )
    op.create_index("ix_crypto_rotation_job_status", "crypto_rotation_job", ["status"])
    op.create_index(
        "ix_crypto_rotation_job_target",
        "crypto_rotation_job",
        ["source_table", "source_column"],
    )


def downgrade() -> None:
    op.drop_index("ix_crypto_rotation_job_target", table_name="crypto_rotation_job")
    op.drop_index("ix_crypto_rotation_job_status", table_name="crypto_rotation_job")
    op.drop_table("crypto_rotation_job")

    op.drop_index("ix_crypto_blind_index_row", table_name="crypto_blind_index")
    op.drop_index("ix_crypto_blind_index_lookup", table_name="crypto_blind_index")
    op.drop_table("crypto_blind_index")

    op.drop_index("ix_crypto_token_access_log_actor", table_name="crypto_token_access_log")
    op.drop_index("ix_crypto_token_access_log_token", table_name="crypto_token_access_log")
    op.drop_table("crypto_token_access_log")

    op.drop_index("ix_crypto_token_vault_data_class", table_name="crypto_token_vault")
    op.drop_index("ix_crypto_token_vault_plaintext_id", table_name="crypto_token_vault")
    op.drop_table("crypto_token_vault")

    op.drop_index("ix_crypto_kek_registry_kek_id_state", table_name="crypto_kek_registry")
    op.drop_table("crypto_kek_registry")
