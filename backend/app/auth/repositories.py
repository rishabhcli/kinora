"""Repositories for the auth/security tables (kinora.md §6, §12).

Each repository wraps an :class:`AsyncSession` and owns the queries for one auth
aggregate. Following the project convention they **flush** (to populate
defaults / surface constraint errors) but never **commit** — the unit-of-work
boundary (``container.session_factory`` / ``app.db.session.get_session``) owns
the transaction.

All repos are deliberately thin and side-effect free beyond their table; the
orchestration (rotation chains, lockout policy, audit emission) lives in the
service layer (:mod:`app.auth.service`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, Result, delete, func, select, update

from app.db.base import new_id
from app.db.models.auth import (
    ApiKey,
    AuthAuditLog,
    AuthCredential,
    AuthSession,
    Permission,
    RecoveryCode,
    RefreshToken,
    Role,
    RoleBinding,
    RolePermission,
)
from app.db.models.enums import AuthEventType, MfaMethod
from app.db.repositories.base import BaseRepository


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _rowcount(result: Result[Any]) -> int:
    """Read the affected-row count from a DML result (typed for mypy)."""
    return int(cast("CursorResult[Any]", result).rowcount or 0)


# --------------------------------------------------------------------------- #
# Per-user credentials
# --------------------------------------------------------------------------- #


class AuthCredentialRepo(BaseRepository):
    """The per-user security row (MFA, lockout, tenant, disabled)."""

    async def get(self, user_id: str) -> AuthCredential | None:
        """Fetch the credential row for ``user_id`` (``None`` if not yet created)."""
        stmt = select(AuthCredential).where(AuthCredential.user_id == user_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def get_or_create(self, user_id: str, *, scheme: str = "bcrypt") -> AuthCredential:
        """Return the credential row, creating an empty one on first access."""
        existing = await self.get(user_id)
        if existing is not None:
            return existing
        row = AuthCredential(id=new_id(), user_id=user_id, password_scheme=scheme)
        self.session.add(row)
        await self.session.flush()
        return row

    async def set_mfa(
        self, user_id: str, *, secret: str | None, enabled: bool, method: MfaMethod | None
    ) -> AuthCredential:
        """Set the MFA secret/enabled/method on the user's credential row."""
        row = await self.get_or_create(user_id)
        row.totp_secret = secret
        row.mfa_enabled = enabled
        row.mfa_method = method
        await self.session.flush()
        return row

    async def record_login_success(self, user_id: str) -> None:
        """Reset lockout counters and stamp ``last_login_at``."""
        row = await self.get_or_create(user_id)
        row.failed_login_count = 0
        row.locked_until = None
        row.last_login_at = _utcnow()
        await self.session.flush()

    async def record_login_failure(
        self, user_id: str, *, max_failures: int, lockout_s: int
    ) -> AuthCredential:
        """Increment the failure counter and lock the account past ``max_failures``."""
        row = await self.get_or_create(user_id)
        row.failed_login_count += 1
        row.last_failed_login_at = _utcnow()
        if row.failed_login_count >= max_failures:
            from datetime import timedelta

            row.locked_until = _utcnow() + timedelta(seconds=lockout_s)
        await self.session.flush()
        return row

    async def set_disabled(self, user_id: str, disabled: bool) -> AuthCredential:
        """Administratively enable/disable an account."""
        row = await self.get_or_create(user_id)
        row.disabled = disabled
        await self.session.flush()
        return row

    async def set_tenant(self, user_id: str, tenant_id: str | None) -> AuthCredential:
        """Set the user's home tenant."""
        row = await self.get_or_create(user_id)
        row.tenant_id = tenant_id
        await self.session.flush()
        return row

    async def mark_password_changed(self, user_id: str, *, scheme: str) -> None:
        """Stamp the password-change time + the scheme used."""
        row = await self.get_or_create(user_id)
        row.password_changed_at = _utcnow()
        row.password_scheme = scheme
        await self.session.flush()


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


class AuthSessionRepo(BaseRepository):
    """Logged-in sessions (device tracking + revocation)."""

    async def create(
        self,
        *,
        user_id: str,
        device_label: str | None = None,
        user_agent: str | None = None,
        ip_address: str | None = None,
        fingerprint: str | None = None,
        expires_at: datetime | None = None,
        session_id: str | None = None,
    ) -> AuthSession:
        """Insert a new auth session row."""
        row = AuthSession(
            id=session_id or new_id(),
            user_id=user_id,
            device_label=device_label,
            user_agent=user_agent,
            ip_address=ip_address,
            device_fingerprint=fingerprint,
            last_seen_at=_utcnow(),
            expires_at=expires_at,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, session_id: str) -> AuthSession | None:
        """Fetch a session by id."""
        return await self.session.get(AuthSession, session_id)

    async def list_active(self, user_id: str) -> Sequence[AuthSession]:
        """List a user's non-revoked sessions, newest first."""
        stmt = (
            select(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
            .order_by(AuthSession.created_at.desc())
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def count_active(self, user_id: str) -> int:
        """Count a user's active sessions (for the per-user cap)."""
        stmt = (
            select(func.count())
            .select_from(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def touch(self, session_id: str, *, ip: str | None = None) -> None:
        """Update ``last_seen_at`` (and optionally the IP) on activity."""
        row = await self.get(session_id)
        if row is None:
            return
        row.last_seen_at = _utcnow()
        if ip:
            row.ip_address = ip
        await self.session.flush()

    async def revoke(self, session_id: str, *, reason: str = "revoked") -> bool:
        """Revoke one session; return whether it was active before."""
        row = await self.get(session_id)
        if row is None or row.revoked_at is not None:
            return False
        row.revoked_at = _utcnow()
        row.revoked_reason = reason
        await self.session.flush()
        return True

    async def revoke_all(
        self, user_id: str, *, reason: str = "logout_all", keep: str | None = None
    ) -> int:
        """Revoke every active session for a user (optionally keeping ``keep``)."""
        stmt = (
            update(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=_utcnow(), revoked_reason=reason)
        )
        if keep is not None:
            stmt = stmt.where(AuthSession.id != keep)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return _rowcount(result)

    async def is_active(self, session_id: str) -> bool:
        """Whether a session exists and is neither revoked nor expired."""
        row = await self.get(session_id)
        if row is None or row.revoked_at is not None:
            return False
        return not (row.expires_at is not None and row.expires_at <= _utcnow())

    async def oldest_active(self, user_id: str) -> AuthSession | None:
        """The oldest active session (evicted when the per-user cap is hit)."""
        stmt = (
            select(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
            .order_by(AuthSession.created_at.asc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()


# --------------------------------------------------------------------------- #
# Refresh tokens (rotation families + reuse detection)
# --------------------------------------------------------------------------- #


class RefreshTokenRepo(BaseRepository):
    """Opaque refresh tokens in rotation families."""

    async def create(
        self,
        *,
        user_id: str,
        token_digest: str,
        family_id: str,
        expires_at: datetime,
        session_id: str | None = None,
    ) -> RefreshToken:
        """Insert a refresh-token row (digest only)."""
        row = RefreshToken(
            id=new_id(),
            user_id=user_id,
            session_id=session_id,
            token_digest=token_digest,
            family_id=family_id,
            expires_at=expires_at,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_by_digest(self, token_digest: str) -> RefreshToken | None:
        """Look up a refresh token by its stored digest."""
        stmt = select(RefreshToken).where(RefreshToken.token_digest == token_digest)
        return (await self.session.execute(stmt)).scalars().first()

    async def mark_used(self, row: RefreshToken, *, replaced_by: str | None) -> None:
        """Consume a token (rotation): stamp ``used_at`` + the successor id."""
        row.used_at = _utcnow()
        row.replaced_by = replaced_by
        await self.session.flush()

    async def revoke_family(self, family_id: str, *, reason: str = "revoked") -> int:
        """Revoke every still-active token in a family (reuse / logout)."""
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=_utcnow())
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return _rowcount(result)

    async def revoke_for_user(self, user_id: str) -> int:
        """Revoke all of a user's refresh tokens (logout-all / password change)."""
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=_utcnow())
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return _rowcount(result)

    async def revoke_for_session(self, session_id: str) -> int:
        """Revoke all refresh tokens bound to a session."""
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.session_id == session_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=_utcnow())
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return _rowcount(result)

    async def purge_expired(self, *, before: datetime | None = None) -> int:
        """Delete expired token rows (housekeeping)."""
        cutoff = before or _utcnow()
        result = await self.session.execute(
            delete(RefreshToken).where(RefreshToken.expires_at < cutoff)
        )
        await self.session.flush()
        return _rowcount(result)


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #


class ApiKeyRepo(BaseRepository):
    """Scoped API keys for headless callers."""

    async def create(
        self,
        *,
        user_id: str,
        key_id: str,
        secret_digest: str,
        name: str,
        scopes: list[str],
        display_prefix: str | None = None,
        tenant_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> ApiKey:
        """Insert an API-key row (digest only)."""
        row = ApiKey(
            id=new_id(),
            user_id=user_id,
            key_id=key_id,
            secret_digest=secret_digest,
            display_prefix=display_prefix,
            name=name,
            scopes=scopes,
            tenant_id=tenant_id,
            expires_at=expires_at,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_by_key_id(self, key_id: str) -> ApiKey | None:
        """O(1) lookup of an API key by its public handle."""
        stmt = select(ApiKey).where(ApiKey.key_id == key_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def get(self, api_key_id: str) -> ApiKey | None:
        """Fetch an API key by its row id."""
        return await self.session.get(ApiKey, api_key_id)

    async def list_for_user(
        self, user_id: str, *, include_revoked: bool = False
    ) -> Sequence[ApiKey]:
        """List a user's API keys, newest first."""
        stmt = select(ApiKey).where(ApiKey.user_id == user_id)
        if not include_revoked:
            stmt = stmt.where(ApiKey.revoked_at.is_(None))
        stmt = stmt.order_by(ApiKey.created_at.desc())
        return (await self.session.execute(stmt)).scalars().all()

    async def touch(self, api_key_id: str) -> None:
        """Stamp ``last_used_at`` on a successful key auth."""
        row = await self.get(api_key_id)
        if row is not None:
            row.last_used_at = _utcnow()
            await self.session.flush()

    async def revoke(self, api_key_id: str, *, user_id: str | None = None) -> bool:
        """Revoke a key (optionally asserting the owner); return success."""
        row = await self.get(api_key_id)
        if row is None or (user_id is not None and row.user_id != user_id):
            return False
        if row.revoked_at is not None:
            return False
        row.revoked_at = _utcnow()
        await self.session.flush()
        return True


# --------------------------------------------------------------------------- #
# RBAC
# --------------------------------------------------------------------------- #


class RbacRepo(BaseRepository):
    """Roles, permissions, and bindings."""

    # -- permissions -- #

    async def upsert_permission(self, name: str, *, description: str | None = None) -> Permission:
        """Get-or-create a permission by name."""
        existing = await self.get_permission(name)
        if existing is not None:
            return existing
        row = Permission(id=new_id(), name=name, description=description)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_permission(self, name: str) -> Permission | None:
        stmt = select(Permission).where(Permission.name == name)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_permissions(self) -> Sequence[Permission]:
        return (
            (await self.session.execute(select(Permission).order_by(Permission.name)))
            .scalars()
            .all()
        )

    # -- roles -- #

    async def upsert_role(
        self, name: str, *, description: str | None = None, is_system: bool = False
    ) -> Role:
        """Get-or-create a role by name."""
        existing = await self.get_role(name)
        if existing is not None:
            return existing
        row = Role(id=new_id(), name=name, description=description, is_system=is_system)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_role(self, name: str) -> Role | None:
        stmt = select(Role).where(Role.name == name)
        return (await self.session.execute(stmt)).scalars().first()

    async def get_role_by_id(self, role_id: str) -> Role | None:
        return await self.session.get(Role, role_id)

    async def list_roles(self) -> Sequence[Role]:
        return (await self.session.execute(select(Role).order_by(Role.name))).scalars().all()

    async def add_permission_to_role(self, role_id: str, permission_id: str) -> RolePermission:
        """Grant a permission to a role (idempotent)."""
        stmt = select(RolePermission).where(
            RolePermission.role_id == role_id, RolePermission.permission_id == permission_id
        )
        existing = (await self.session.execute(stmt)).scalars().first()
        if existing is not None:
            return existing
        row = RolePermission(id=new_id(), role_id=role_id, permission_id=permission_id)
        self.session.add(row)
        await self.session.flush()
        return row

    async def permissions_for_role(self, role_id: str) -> list[str]:
        """The permission names a role grants."""
        stmt = (
            select(Permission.name)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == role_id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # -- bindings -- #

    async def bind_role(
        self,
        *,
        user_id: str,
        role_id: str,
        tenant_id: str | None = None,
        granted_by: str | None = None,
    ) -> RoleBinding:
        """Bind a role to a user (idempotent per (user, role, tenant))."""
        stmt = select(RoleBinding).where(
            RoleBinding.user_id == user_id,
            RoleBinding.role_id == role_id,
            RoleBinding.tenant_id.is_(tenant_id)
            if tenant_id is None
            else RoleBinding.tenant_id == tenant_id,
        )
        existing = (await self.session.execute(stmt)).scalars().first()
        if existing is not None:
            return existing
        row = RoleBinding(
            id=new_id(),
            user_id=user_id,
            role_id=role_id,
            tenant_id=tenant_id,
            granted_by=granted_by,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def unbind_role(
        self, *, user_id: str, role_id: str, tenant_id: str | None = None
    ) -> bool:
        """Remove a role binding; return whether one was removed."""
        stmt = delete(RoleBinding).where(
            RoleBinding.user_id == user_id,
            RoleBinding.role_id == role_id,
            RoleBinding.tenant_id.is_(tenant_id)
            if tenant_id is None
            else RoleBinding.tenant_id == tenant_id,
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return bool(_rowcount(result))

    async def roles_for_user(self, user_id: str) -> list[tuple[str, str | None]]:
        """Return ``(role_name, tenant_id)`` pairs bound to a user."""
        stmt = (
            select(Role.name, RoleBinding.tenant_id)
            .join(RoleBinding, RoleBinding.role_id == Role.id)
            .where(RoleBinding.user_id == user_id)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(str(name), tenant) for name, tenant in rows]

    async def permissions_for_user(self, user_id: str) -> set[str]:
        """The union of all permission names a user holds via their roles."""
        stmt = (
            select(Permission.name)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .join(RoleBinding, RoleBinding.role_id == Role.id)
            .where(RoleBinding.user_id == user_id)
        )
        return set((await self.session.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------- #
# Recovery codes
# --------------------------------------------------------------------------- #


class RecoveryCodeRepo(BaseRepository):
    """Single-use MFA recovery codes (digest only)."""

    async def replace_all(self, user_id: str, digests: Sequence[str]) -> None:
        """Delete a user's existing codes and insert a fresh batch."""
        await self.session.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
        for digest in digests:
            self.session.add(RecoveryCode(id=new_id(), user_id=user_id, code_digest=digest))
        await self.session.flush()

    async def consume(self, user_id: str, code_digest: str) -> bool:
        """Consume an unused code matching ``code_digest``; return success."""
        stmt = select(RecoveryCode).where(
            RecoveryCode.user_id == user_id,
            RecoveryCode.code_digest == code_digest,
            RecoveryCode.used_at.is_(None),
        )
        row = (await self.session.execute(stmt)).scalars().first()
        if row is None:
            return False
        row.used_at = _utcnow()
        await self.session.flush()
        return True

    async def count_unused(self, user_id: str) -> int:
        """How many recovery codes remain unused."""
        stmt = (
            select(func.count())
            .select_from(RecoveryCode)
            .where(RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None))
        )
        return int((await self.session.execute(stmt)).scalar_one())


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #


class AuthAuditRepo(BaseRepository):
    """The append-only security audit log."""

    async def record(
        self,
        event: AuthEventType,
        *,
        user_id: str | None = None,
        success: bool = True,
        ip_address: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AuthAuditLog:
        """Append one audit record."""
        row = AuthAuditLog(
            id=new_id(),
            user_id=user_id,
            event=event,
            success=success,
            ip_address=ip_address,
            user_agent=user_agent,
            session_id=session_id,
            detail=detail,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_for_user(
        self, user_id: str, *, limit: int = 50, event: AuthEventType | None = None
    ) -> Sequence[AuthAuditLog]:
        """Recent audit records for a user, newest first."""
        stmt = select(AuthAuditLog).where(AuthAuditLog.user_id == user_id)
        if event is not None:
            stmt = stmt.where(AuthAuditLog.event == event)
        stmt = stmt.order_by(AuthAuditLog.created_at.desc()).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()

    async def purge_older_than(self, cutoff: datetime) -> int:
        """Delete audit rows older than ``cutoff`` (retention sweep)."""
        result = await self.session.execute(
            delete(AuthAuditLog).where(AuthAuditLog.created_at < cutoff)
        )
        await self.session.flush()
        return _rowcount(result)


__all__ = [
    "ApiKeyRepo",
    "AuthAuditRepo",
    "AuthCredentialRepo",
    "AuthSessionRepo",
    "RbacRepo",
    "RecoveryCodeRepo",
    "RefreshTokenRepo",
]
