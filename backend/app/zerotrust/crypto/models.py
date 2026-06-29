"""Persistent layer for the data-at-rest crypto facet — five additive tables.

These tables let the in-memory primitives above run durably and auditably in
production. They are **additive**: no existing table is modified, and every name
is namespaced ``crypto_*`` so it cannot collide with a sibling facet's schema.

* :class:`CryptoKekRegistry` — one row per (KEK id, version): its lifecycle state
  and provenance. The KMS owns the *material*; this is the queryable catalogue a
  rotation job and an auditor read (which versions exist, which is active, which
  are draining toward destruction).
* :class:`CryptoTokenVault` — the tokenization vault store: token → encrypted
  plaintext (envelope + wrapped DEK) + policy. Indexed by token (unique) and by a
  keyed plaintext id for deterministic-token dedup.
* :class:`CryptoTokenAccessLog` — append-only audit of every detokenize attempt
  (actor, purpose, allow/deny). Detokenization reveals PII, so each attempt is a
  recorded event.
* :class:`CryptoBlindIndex` — companion blind-index tokens for searchable
  encrypted columns (one row per (table, column, row, token)), so equality/
  prefix/range queries are plain indexed lookups against keyed tokens.
* :class:`CryptoRotationJob` — the durable cursor/ledger for an online rotation
  pass (which column, which mode, progress counters, checkpoint), so a long
  re-encryption survives restarts and is resumable.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Identity,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin, TimestampMixin
from app.db.models.enums import str_enum

# --------------------------------------------------------------------------- #
# Enums (stored as portable VARCHAR + CHECK, matching the rest of the schema)
# --------------------------------------------------------------------------- #


class KekLifecycle(enum.StrEnum):
    """KEK-version lifecycle, mirroring :class:`crypto.keys.KeyState`."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    PENDING_DELETION = "pending_deletion"
    DESTROYED = "destroyed"


class TokenSchemeEnum(enum.StrEnum):
    """How a vault token was produced."""

    RANDOM = "random"
    DETERMINISTIC = "deterministic"


class RotationMode(enum.StrEnum):
    """Which rotation a job performs."""

    REWRAP = "rewrap"  # cheap: re-wrap DEKs under a new KEK version
    REENCRYPT = "reencrypt"  # full: decrypt + re-encrypt under fresh DEKs


class RotationStatus(enum.StrEnum):
    """Lifecycle of a rotation job."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class BlindIndexKind(enum.StrEnum):
    """Which search transform a blind-index token represents."""

    EQUALITY = "equality"
    PREFIX = "prefix"
    RANGE = "range"


# --------------------------------------------------------------------------- #
# Key registry
# --------------------------------------------------------------------------- #


class CryptoKekRegistry(StrIdMixin, TimestampMixin, Base):
    """Catalogue of KEK versions: state + provenance (material stays in the KMS)."""

    __tablename__ = "crypto_kek_registry"
    __table_args__ = (
        UniqueConstraint("kek_id", "version", name="uq_crypto_kek_registry_kek_id_version"),
        Index("ix_crypto_kek_registry_kek_id_state", "kek_id", "state"),
    )

    kek_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[KekLifecycle] = mapped_column(
        str_enum(KekLifecycle, "ck_crypto_kek_registry_state"), nullable=False
    )
    #: True for exactly one version per kek_id — the version new writes encrypt under.
    is_active: Mapped[bool] = mapped_column(nullable=False, default=False)
    #: Free-form provenance (provider, hsm key arn, rotation reason).
    provenance: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


# --------------------------------------------------------------------------- #
# Tokenization vault
# --------------------------------------------------------------------------- #


class CryptoTokenVault(StrIdMixin, TimestampMixin, Base):
    """token → encrypted plaintext + policy. The detokenize side of the vault."""

    __tablename__ = "crypto_token_vault"
    __table_args__ = (
        UniqueConstraint("token", name="uq_crypto_token_vault_token"),
        Index("ix_crypto_token_vault_plaintext_id", "plaintext_id"),
        Index("ix_crypto_token_vault_data_class", "data_class"),
    )

    #: The surrogate token (format-preserving). Unique; the lookup key.
    token: Mapped[str] = mapped_column(String(256), nullable=False)
    #: Keyed id of the plaintext+format, for deterministic-token dedup (nullable
    #: for random tokens, which are never deduped).
    plaintext_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The AEAD envelope protecting the plaintext at rest.
    envelope: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: The wrapped DEK that opens the envelope (kek_id/version/alg/ciphertext, JSON).
    wrapped_dek: Mapped[dict] = mapped_column(JSONB, nullable=False)
    scheme: Mapped[TokenSchemeEnum] = mapped_column(
        str_enum(TokenSchemeEnum, "ck_crypto_token_vault_scheme"), nullable=False
    )
    #: Purposes permitted to detokenize (a JSON array; empty = write-only PII).
    allowed_purposes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    data_class: Mapped[str] = mapped_column(String(64), nullable=False, default="pii")


class CryptoTokenAccessLog(CreatedAtMixin, Base):
    """Append-only audit of detokenize attempts (allow + deny both recorded)."""

    __tablename__ = "crypto_token_access_log"
    __table_args__ = (
        Index("ix_crypto_token_access_log_token", "token"),
        Index("ix_crypto_token_access_log_actor", "actor"),
    )

    #: Monotonic surrogate PK — this is a high-write append-only ledger.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    token: Mapped[str] = mapped_column(String(256), nullable=False)
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    purpose: Mapped[str] = mapped_column(String(128), nullable=False)
    allowed: Mapped[bool] = mapped_column(nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


# --------------------------------------------------------------------------- #
# Searchable-encryption companion index
# --------------------------------------------------------------------------- #


class CryptoBlindIndex(Base):
    """A blind-index token row for one searchable encrypted cell.

    A single cell may have several rows (e.g. many prefix tokens), so the PK is a
    surrogate and ``(source_table, source_column, kind, token)`` is indexed for
    the lookup ``WHERE ... token = :probe``. ``source_row_id`` ties a hit back to
    the owning row.
    """

    __tablename__ = "crypto_blind_index"
    __table_args__ = (
        Index(
            "ix_crypto_blind_index_lookup",
            "source_table",
            "source_column",
            "kind",
            "token",
        ),
        Index("ix_crypto_blind_index_row", "source_table", "source_row_id"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source_table: Mapped[str] = mapped_column(String(128), nullable=False)
    source_column: Mapped[str] = mapped_column(String(128), nullable=False)
    source_row_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[BlindIndexKind] = mapped_column(
        str_enum(BlindIndexKind, "ck_crypto_blind_index_kind"), nullable=False
    )
    #: The keyed, truncated HMAC token (raw bytes).
    token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


# --------------------------------------------------------------------------- #
# Rotation job ledger
# --------------------------------------------------------------------------- #


class CryptoRotationJob(StrIdMixin, TimestampMixin, Base):
    """Durable, resumable cursor + counters for an online rotation pass."""

    __tablename__ = "crypto_rotation_job"
    __table_args__ = (
        Index("ix_crypto_rotation_job_status", "status"),
        Index("ix_crypto_rotation_job_target", "source_table", "source_column"),
    )

    mode: Mapped[RotationMode] = mapped_column(
        str_enum(RotationMode, "ck_crypto_rotation_job_mode"), nullable=False
    )
    status: Mapped[RotationStatus] = mapped_column(
        str_enum(RotationStatus, "ck_crypto_rotation_job_status"),
        nullable=False,
        default=RotationStatus.PENDING,
    )
    source_table: Mapped[str] = mapped_column(String(128), nullable=False)
    source_column: Mapped[str] = mapped_column(String(128), nullable=False)
    kek_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: The KEK version this pass is draining *to* (rewrap) or the new policy tag.
    target_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Opaque resume checkpoint (e.g. last-processed row id / keyset cursor).
    cursor: Mapped[str | None] = mapped_column(String(256), nullable=True)
    scanned: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    rotated: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = [
    "BlindIndexKind",
    "CryptoBlindIndex",
    "CryptoKekRegistry",
    "CryptoRotationJob",
    "CryptoTokenAccessLog",
    "CryptoTokenVault",
    "KekLifecycle",
    "RotationMode",
    "RotationStatus",
    "TokenSchemeEnum",
]
