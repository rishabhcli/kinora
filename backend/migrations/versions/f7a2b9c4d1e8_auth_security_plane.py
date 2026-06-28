"""auth & security plane — credentials, sessions, refresh tokens, RBAC, MFA, audit

Revision ID: f7a2b9c4d1e8
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28 12:00:00.000000

Creates the ten additive tables backing the production auth/security system
(kinora.md §6, §12). Every table hangs off ``users`` and touches no existing
schema, so this migration is purely additive and fully reversible:

* ``auth_credentials`` — per-user MFA / lockout / password metadata / tenant.
* ``auth_sessions``     — logged-in sessions with device tracking + revocation.
* ``refresh_tokens``    — opaque refresh tokens in rotation families (reuse det.).
* ``api_keys``          — scoped API keys for headless callers.
* ``permissions`` / ``roles`` / ``role_permissions`` / ``role_bindings`` — RBAC.
* ``recovery_codes``    — single-use MFA recovery codes (digest only).
* ``auth_audit_log``    — append-only security event log.

The portable enum columns (``mfa_method``, ``auth_event_type``) are stored as
``VARCHAR`` + a named ``CHECK`` constraint to match the rest of the schema
(``native_enum=False``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f7a2b9c4d1e8"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_MFA_METHODS = ("totp", "recovery")
_AUTH_EVENTS = (
    "register",
    "login_success",
    "login_failure",
    "login_locked",
    "logout",
    "logout_all",
    "token_refresh",
    "token_reuse_detected",
    "password_change",
    "password_reset_request",
    "password_reset",
    "mfa_enroll",
    "mfa_enable",
    "mfa_disable",
    "mfa_challenge_success",
    "mfa_challenge_failure",
    "recovery_code_used",
    "recovery_codes_regenerated",
    "api_key_create",
    "api_key_revoke",
    "api_key_used",
    "session_revoke",
    "role_grant",
    "role_revoke",
    "account_disabled",
    "account_enabled",
)


def upgrade() -> None:
    # -- RBAC catalog (no user FK) ------------------------------------------ #
    op.create_table(
        "permissions",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permissions")),
        sa.UniqueConstraint("name", name="uq_permissions_name"),
    )
    op.create_table(
        "roles",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
        sa.UniqueConstraint("name", name="uq_roles_name"),
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.String(length=64), nullable=False),
        sa.Column("permission_id", sa.String(length=64), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name=op.f("fk_role_permissions_role_id_roles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            name=op.f("fk_role_permissions_permission_id_permissions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role_permissions")),
        sa.UniqueConstraint("role_id", "permission_id", name="uq_role_permissions_role_id_perm"),
    )
    op.create_index(
        op.f("ix_role_permissions_role_id"), "role_permissions", ["role_id"], unique=False
    )
    op.create_table(
        "role_bindings",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("role_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("granted_by", sa.String(length=64), nullable=True),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_role_bindings_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name=op.f("fk_role_bindings_role_id_roles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role_bindings")),
        sa.UniqueConstraint(
            "user_id", "role_id", "tenant_id", name="uq_role_bindings_user_role_tenant"
        ),
    )
    op.create_index(op.f("ix_role_bindings_user_id"), "role_bindings", ["user_id"], unique=False)

    # -- per-user security state -------------------------------------------- #
    op.create_table(
        "auth_credentials",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("password_scheme", sa.String(length=32), nullable=False),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mfa_enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "mfa_method",
            sa.Enum(*_MFA_METHODS, name="mfa_method", native_enum=False),
            nullable=True,
        ),
        sa.Column("totp_secret", sa.String(length=128), nullable=True),
        sa.Column("failed_login_count", sa.Integer(), nullable=False),
        sa.Column("last_failed_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_credentials_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_credentials")),
        sa.UniqueConstraint("user_id", name="uq_auth_credentials_user_id"),
    )
    op.create_index(
        op.f("ix_auth_credentials_tenant_id"), "auth_credentials", ["tenant_id"], unique=False
    )

    # -- sessions ----------------------------------------------------------- #
    op.create_table(
        "auth_sessions",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("device_label", sa.String(length=128), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("device_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=64), nullable=True),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
    )
    op.create_index(op.f("ix_auth_sessions_user_id"), "auth_sessions", ["user_id"], unique=False)
    op.create_index(
        "ix_auth_sessions_user_active", "auth_sessions", ["user_id", "revoked_at"], unique=False
    )

    # -- refresh tokens ----------------------------------------------------- #
    op.create_table(
        "refresh_tokens",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("family_id", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by", sa.String(length=64), nullable=True),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_refresh_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["auth_sessions.id"],
            name=op.f("fk_refresh_tokens_session_id_auth_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
        sa.UniqueConstraint("token_digest", name="uq_refresh_tokens_token_digest"),
    )
    op.create_index(
        op.f("ix_refresh_tokens_family_id"), "refresh_tokens", ["family_id"], unique=False
    )
    op.create_index(op.f("ix_refresh_tokens_user_id"), "refresh_tokens", ["user_id"], unique=False)

    # -- api keys ----------------------------------------------------------- #
    op.create_table(
        "api_keys",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("secret_digest", sa.String(length=64), nullable=False),
        sa.Column("display_prefix", sa.String(length=16), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_api_keys_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("key_id", name="uq_api_keys_key_id"),
    )
    op.create_index(op.f("ix_api_keys_user_id"), "api_keys", ["user_id"], unique=False)

    # -- recovery codes ----------------------------------------------------- #
    op.create_table(
        "recovery_codes",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("code_digest", sa.String(length=64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_recovery_codes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recovery_codes")),
        sa.UniqueConstraint("code_digest", name="uq_recovery_codes_code_digest"),
    )
    op.create_index(op.f("ix_recovery_codes_user_id"), "recovery_codes", ["user_id"], unique=False)

    # -- audit log ---------------------------------------------------------- #
    op.create_table(
        "auth_audit_log",
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column(
            "event",
            sa.Enum(*_AUTH_EVENTS, name="auth_event_type", native_enum=False),
            nullable=False,
        ),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_audit_log_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_audit_log")),
    )
    op.create_index(op.f("ix_auth_audit_log_user_id"), "auth_audit_log", ["user_id"], unique=False)
    op.create_index(
        "ix_auth_audit_log_event_created", "auth_audit_log", ["event", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_auth_audit_log_event_created", table_name="auth_audit_log")
    op.drop_index(op.f("ix_auth_audit_log_user_id"), table_name="auth_audit_log")
    op.drop_table("auth_audit_log")

    op.drop_index(op.f("ix_recovery_codes_user_id"), table_name="recovery_codes")
    op.drop_table("recovery_codes")

    op.drop_index(op.f("ix_api_keys_user_id"), table_name="api_keys")
    op.drop_table("api_keys")

    op.drop_index(op.f("ix_refresh_tokens_user_id"), table_name="refresh_tokens")
    op.drop_index(op.f("ix_refresh_tokens_family_id"), table_name="refresh_tokens")
    op.drop_table("refresh_tokens")

    op.drop_index("ix_auth_sessions_user_active", table_name="auth_sessions")
    op.drop_index(op.f("ix_auth_sessions_user_id"), table_name="auth_sessions")
    op.drop_table("auth_sessions")

    op.drop_index(op.f("ix_auth_credentials_tenant_id"), table_name="auth_credentials")
    op.drop_table("auth_credentials")

    op.drop_index(op.f("ix_role_bindings_user_id"), table_name="role_bindings")
    op.drop_table("role_bindings")

    op.drop_index(op.f("ix_role_permissions_role_id"), table_name="role_permissions")
    op.drop_table("role_permissions")

    op.drop_table("roles")
    op.drop_table("permissions")
