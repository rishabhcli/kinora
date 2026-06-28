"""Auth & security ORM models (kinora.md §6, §12 — the security plane).

Every table here is **additive**: the existing ``users`` table is untouched, and
all the new state hangs off it via ``user_id`` foreign keys with
``ON DELETE CASCADE`` (a deleted user's credentials, sessions, keys, MFA, and
recovery codes go with them; the audit log uses ``SET NULL`` so the security
trail survives the account it describes).

Tables:

* :class:`AuthCredential` — per-user security state that doesn't belong on the
  lean ``users`` row: MFA secret/enabled, lockout counters, password metadata,
  tenant id, disabled flag.
* :class:`RefreshToken` — opaque refresh-token rows in **rotation families** for
  reuse detection (a replayed token revokes its whole family).
* :class:`AuthSession` — a logged-in session with device tracking + revocation.
* :class:`ApiKey` — first-class scoped API keys for headless callers.
* :class:`Role` / :class:`Permission` / :class:`RolePermission` / :class:`RoleBinding`
  — the RBAC graph (roles hold permissions; users are bound to roles, optionally
  scoped to a tenant).
* :class:`RecoveryCode` — single-use MFA recovery codes (digest only).
* :class:`AuthAuditLog` — the append-only security event log.

The portable ``VARCHAR``+``CHECK`` enum pattern (``str_enum``) and the shared
mixins are reused from the rest of the schema so Alembic autogenerate stays
stable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin, TimestampMixin
from app.db.models.enums import AuthEventType, MfaMethod, str_enum

# --------------------------------------------------------------------------- #
# Per-user security state
# --------------------------------------------------------------------------- #


class AuthCredential(StrIdMixin, TimestampMixin, Base):
    """Per-user security state (one row per user; lazily created on first need).

    Keeps the lean ``users`` table unchanged while giving the auth system a home
    for MFA, lockout counters, password metadata, tenant membership, and the
    disabled flag.
    """

    __tablename__ = "auth_credentials"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_auth_credentials_user_id"),
        Index("ix_auth_credentials_tenant_id", "tenant_id"),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: The hashing scheme that produced ``users.hashed_password`` ("bcrypt"/"argon2").
    password_scheme: Mapped[str] = mapped_column(String(32), default="bcrypt", nullable=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # -- MFA / TOTP -- #
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_method: Mapped[MfaMethod | None] = mapped_column(
        str_enum(MfaMethod, "mfa_method"), nullable=True
    )
    #: The base32 TOTP shared secret (only set while/after enrolment).
    totp_secret: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # -- lockout / throttling -- #
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_failed_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # -- account state -- #
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Per-tenant isolation: a user belongs to at most one home tenant.
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- #
# Sessions + refresh tokens
# --------------------------------------------------------------------------- #


class AuthSession(StrIdMixin, TimestampMixin, Base):
    """A logged-in session with device tracking and revocation (kinora.md §6).

    Distinct from the *reading* ``sessions`` table (scheduler state): this is the
    **authentication** session, the unit a user sees in "active devices" and can
    revoke. Access tokens carry its id as ``sid`` so revoking the session
    invalidates its tokens at refresh time and via the access-token denylist.
    """

    __tablename__ = "auth_sessions"
    __table_args__ = (
        Index("ix_auth_sessions_user_id", "user_id"),
        Index("ix_auth_sessions_user_active", "user_id", "revoked_at"),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: Coarse device label for the UI ("Chrome on macOS").
    device_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Stable per-device fingerprint (UA+IP digest) for display/grouping.
    device_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Why the session ended ("logout", "revoked", "reuse_detected", "expired").
    revoked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RefreshToken(StrIdMixin, CreatedAtMixin, Base):
    """An opaque refresh token in a rotation family (kinora.md §12 security).

    Only the SHA-256 ``token_digest`` is stored. Each refresh **rotates**: the
    presented token is marked ``used_at`` and a child is issued in the same
    ``family_id``. Presenting an already-``used`` token is a replay → the service
    revokes the entire family (a stolen-token breach signal).
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        UniqueConstraint("token_digest", name="uq_refresh_tokens_token_digest"),
        Index("ix_refresh_tokens_family_id", "family_id"),
        Index("ix_refresh_tokens_user_id", "user_id"),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("auth_sessions.id", ondelete="CASCADE"), nullable=True
    )
    #: SHA-256 of the opaque token string (the lookup key; plaintext never stored).
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    #: All tokens in one rotation chain share this id (reuse detection scope).
    family_id: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Set when this token is rotated/consumed; a second use is reuse detection.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Set when the family is revoked (logout, reuse, or admin action).
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The id of the token this one rotated into (forensic chain).
    replaced_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #


class ApiKey(StrIdMixin, CreatedAtMixin, Base):
    """A scoped API key for headless callers (kinora.md §6/§12).

    Verified in O(1) by ``key_id`` (a public handle embedded in the presented
    key); the secret is checked against ``secret_digest`` (an HMAC keyed by the
    server-side pepper) in constant time. Scopes gate what the key can do, fully
    independent of the owning user's interactive roles.
    """

    __tablename__ = "api_keys"
    __table_args__ = (
        UniqueConstraint("key_id", name="uq_api_keys_key_id"),
        Index("ix_api_keys_user_id", "user_id"),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: Public, indexable handle embedded in the key (not a secret).
    key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    #: HMAC(pepper, secret) — the stored fingerprint of the secret part.
    secret_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    #: A short clear prefix of the secret for the UI ("kino_sk_abcd…").
    display_prefix: Mapped[str | None] = mapped_column(String(16), nullable=True)
    name: Mapped[str] = mapped_column(String(120), default="api key", nullable=False)
    #: Granted scopes (e.g. ``["books:read", "library:read"]``).
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- #
# RBAC: roles, permissions, bindings
# --------------------------------------------------------------------------- #


class Permission(StrIdMixin, CreatedAtMixin, Base):
    """A single named permission (e.g. ``books:write``, ``admin:rbac``)."""

    __tablename__ = "permissions"
    __table_args__ = (UniqueConstraint("name", name="uq_permissions_name"),)

    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class Role(StrIdMixin, CreatedAtMixin, Base):
    """A named role that bundles permissions (e.g. ``admin``, ``reader``)."""

    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("name", name="uq_roles_name"),)

    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Built-in roles cannot be deleted/renamed through the admin API.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class RolePermission(StrIdMixin, CreatedAtMixin, Base):
    """Join table: which permissions a role grants."""

    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permissions_role_id_perm"),
        Index("ix_role_permissions_role_id", "role_id"),
    )

    role_id: Mapped[str] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    permission_id: Mapped[str] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False
    )


class RoleBinding(StrIdMixin, CreatedAtMixin, Base):
    """A user's grant of a role, optionally scoped to a tenant (per-tenant RBAC)."""

    __tablename__ = "role_bindings"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "role_id", "tenant_id", name="uq_role_bindings_user_role_tenant"
        ),
        Index("ix_role_bindings_user_id", "user_id"),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[str] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    #: NULL == a global grant; a value scopes the role to that tenant only.
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    granted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


# --------------------------------------------------------------------------- #
# Recovery codes + audit log
# --------------------------------------------------------------------------- #


class RecoveryCode(StrIdMixin, CreatedAtMixin, Base):
    """A single-use MFA recovery code (digest only; consumed on first use)."""

    __tablename__ = "recovery_codes"
    __table_args__ = (
        Index("ix_recovery_codes_user_id", "user_id"),
        UniqueConstraint("code_digest", name="uq_recovery_codes_code_digest"),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    code_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuthAuditLog(StrIdMixin, CreatedAtMixin, Base):
    """An append-only security audit record (kinora.md §12 observability).

    ``user_id`` uses ``SET NULL`` so the security trail outlives the account it
    describes (you still want the record that "user X was deleted / locked out").
    """

    __tablename__ = "auth_audit_log"
    __table_args__ = (
        Index("ix_auth_audit_log_user_id", "user_id"),
        Index("ix_auth_audit_log_event_created", "event", "created_at"),
    )

    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    event: Mapped[AuthEventType] = mapped_column(
        str_enum(AuthEventType, "auth_event_type"), nullable=False
    )
    #: Whether the event represents a success (False == a denied/failed attempt).
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Free-form structured context (never secrets) — e.g. {"reason": "..."}.
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


__all__ = [
    "ApiKey",
    "AuthAuditLog",
    "AuthCredential",
    "AuthSession",
    "Permission",
    "RecoveryCode",
    "RefreshToken",
    "Role",
    "RoleBinding",
    "RolePermission",
]
