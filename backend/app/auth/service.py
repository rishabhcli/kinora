"""The :class:`AuthService` orchestrator (kinora.md §6, §12).

This is the single entry point the route layer calls. It composes the crypto
primitives (:mod:`app.core.security`), the token service
(:mod:`app.auth.tokens`), the repositories (:mod:`app.auth.repositories`), the
RBAC catalogue (:mod:`app.auth.rbac`), the login throttle / revocation store
(:mod:`app.auth.lockout`), and the audit log into the high-level flows:

* register / login (password + optional MFA) / refresh / logout / logout-all,
* password change + reset,
* MFA enrol / verify / disable + recovery codes,
* API-key issue / verify / revoke,
* RBAC grant / revoke + the :class:`~app.auth.rbac.Principal` builder,
* session listing / revocation,
* the security audit log.

Each public method opens its **own** unit of work via the injected
``session_factory`` so it is a complete, committing transaction — the route layer
just awaits it. Provider/network calls never happen here; everything is DB +
Redis, so the whole surface is exercised against the isolated test infra.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.errors import (
    AccountDisabled,
    AccountLocked,
    ApiKeyInvalid,
    EmailTaken,
    InvalidCredentials,
    MfaAlreadyEnrolled,
    MfaInvalid,
    MfaNotEnrolled,
    MfaRequired,
    PermissionDenied,
    SessionNotFound,
    TokenInvalid,
    TokenReused,
    WeakPassword,
)
from app.auth.lockout import LoginThrottle, RevocationStore
from app.auth.rbac import (
    DEFAULT_ROLE,
    PERMISSIONS,
    ROLES,
    SYSTEM_ROLES,
    Principal,
    expand_roles_to_permissions,
    normalize_scopes,
)
from app.auth.repositories import (
    ApiKeyRepo,
    AuthAuditRepo,
    AuthCredentialRepo,
    AuthSessionRepo,
    RbacRepo,
    RecoveryCodeRepo,
    RefreshTokenRepo,
)
from app.auth.tokens import IssuedRefreshToken, TokenService
from app.core import security as crypto
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import DeviceInfo, PasswordHasher, PasswordPolicy
from app.db.models.enums import AuthEventType, MfaMethod
from app.db.models.user import User
from app.db.repositories.user import UserRepo

logger = get_logger("app.auth.service")

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


# --------------------------------------------------------------------------- #
# Result value objects
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class TokenBundle:
    """The access + refresh pair returned by login / refresh."""

    access_token: str
    refresh_token: str
    expires_in: int
    session_id: str
    token_type: str = "bearer"  # noqa: S105 - the OAuth token_type label, not a secret


@dataclass(slots=True)
class LoginContext:
    """Per-request context for a login (device + network identity, for audit)."""

    ip: str | None = None
    user_agent: str | None = None


@dataclass(slots=True)
class MfaEnrollment:
    """The data a client needs to finish TOTP enrolment."""

    secret: str
    provisioning_uri: str
    recovery_codes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #


class AuthService:
    """High-level auth flows over the auth repositories + token service."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: SessionFactory,
        hasher: PasswordHasher,
        tokens: TokenService,
        throttle: LoginThrottle,
        revocations: RevocationStore,
        policy: PasswordPolicy | None = None,
    ) -> None:
        self._settings = settings
        self._sf = session_factory
        self._hasher = hasher
        self._tokens = tokens
        self._throttle = throttle
        self._revocations = revocations
        self._policy = policy or _policy_from_settings(settings)

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    async def register(self, email: str, password: str, *, ctx: LoginContext | None = None) -> User:
        """Create an account: validate the password, hash it, assign the default role."""
        problems = self._policy.validate(password)
        if problems:
            raise WeakPassword(problems)
        async with self._sf() as db:
            users = UserRepo(db)
            if await users.get_by_email(email) is not None:
                raise EmailTaken()
            user = await users.create(email=email, hashed_password=self._hasher.hash(password))
            creds = AuthCredentialRepo(db)
            await creds.get_or_create(user.id, scheme=self._hasher.scheme)
            await creds.mark_password_changed(user.id, scheme=self._hasher.scheme)
            # Grant the default role (seeding the catalogue on first use).
            await self._ensure_catalogue(db)
            rbac = RbacRepo(db)
            role = await rbac.get_role(DEFAULT_ROLE)
            if role is not None:
                await rbac.bind_role(user_id=user.id, role_id=role.id)
            await AuthAuditRepo(db).record(
                AuthEventType.REGISTER,
                user_id=user.id,
                ip_address=(ctx.ip if ctx else None),
                user_agent=(ctx.user_agent if ctx else None),
            )
        logger.info("auth.registered", user_id=user.id)
        return user

    # ------------------------------------------------------------------ #
    # Login (password + optional MFA)
    # ------------------------------------------------------------------ #

    async def login(
        self,
        email: str,
        password: str,
        *,
        ctx: LoginContext | None = None,
    ) -> TokenBundle | MfaRequired:
        """Verify credentials. Returns a token bundle, or raises :class:`MfaRequired`.

        When MFA is enabled the password step succeeds but a second factor is
        required: the caller receives an :class:`MfaRequired` carrying a
        short-lived challenge token to be exchanged via :meth:`complete_mfa_login`.
        """
        ctx = ctx or LoginContext()
        await self._guard_ip_throttle(ctx.ip)
        # Phase 1 — verify (read-only): a failure here records its side effect
        # (the lockout counter increment) in its OWN committed transaction so the
        # subsequent ``raise`` cannot roll the increment back. Phase 2 establishes
        # the session in a fresh transaction only on full success.
        async with self._sf() as db:
            user = await UserRepo(db).get_by_email(email)
            if user is None:
                self._hasher.verify(password, _DUMMY_HASH)  # equalise timing
                verdict = "unknown_email"
                user_id = None
            else:
                creds = await AuthCredentialRepo(db).get_or_create(
                    user.id, scheme=self._hasher.scheme
                )
                user_id = user.id
                if creds.disabled:
                    verdict = "disabled"
                elif _is_locked(creds.locked_until):
                    verdict = "locked"
                    locked_retry = _seconds_until(creds.locked_until)
                elif not self._hasher.verify(password, user.hashed_password):
                    verdict = "bad_password"
                elif creds.mfa_enabled:
                    verdict = "mfa"
                else:
                    verdict = "ok"

        if verdict == "unknown_email":
            await self._record_login_failure(None, ctx, reason="unknown_email")
            raise InvalidCredentials()
        if verdict == "disabled":
            await self._record_login_failure(user_id, ctx, reason="disabled", lockout=False)
            raise AccountDisabled()
        if verdict == "locked":
            await self._audit(AuthEventType.LOGIN_LOCKED, user_id=user_id, ctx=ctx, success=False)
            raise AccountLocked(retry_after_s=locked_retry)
        if verdict == "bad_password":
            await self._record_login_failure(user_id, ctx, reason="bad_password")
            raise InvalidCredentials()
        if verdict == "mfa":
            await self._audit(
                AuthEventType.LOGIN_SUCCESS, user_id=user_id, ctx=ctx, detail={"mfa_pending": True}
            )
            assert user_id is not None
            raise MfaRequired(mfa_token=self._tokens.issue_mfa_challenge(user_id))

        # verdict == "ok"
        assert user is not None
        async with self._sf() as db:
            creds_repo = AuthCredentialRepo(db)
            if self._hasher.needs_rehash(user.hashed_password):
                await UserRepo(db).set_password(user.id, self._hasher.hash(password))
                await creds_repo.mark_password_changed(user.id, scheme=self._hasher.scheme)
            await creds_repo.record_login_success(user.id)
            bundle = await self._establish_session(db, user, ctx)
            await AuthAuditRepo(db).record(
                AuthEventType.LOGIN_SUCCESS,
                user_id=user.id,
                session_id=bundle.session_id,
                ip_address=ctx.ip,
                user_agent=ctx.user_agent,
            )
        await self._throttle.reset(ctx.ip or "")
        return bundle

    async def complete_mfa_login(
        self, mfa_token: str, code: str, *, ctx: LoginContext | None = None
    ) -> TokenBundle:
        """Exchange a TOTP/recovery code + challenge token for a session."""
        ctx = ctx or LoginContext()
        user_id = self._tokens.decode_mfa_challenge(mfa_token)
        async with self._sf() as db:
            user = await UserRepo(db).get(user_id)
            if user is None:
                raise InvalidCredentials()
            creds = await AuthCredentialRepo(db).get(user_id)
            audit = AuthAuditRepo(db)
            if creds is None or not creds.mfa_enabled or not creds.totp_secret:
                raise MfaNotEnrolled()
            ok = crypto.verify_totp(
                creds.totp_secret, code, window=self._settings.totp_drift_window
            )
            if not ok:
                # Fall back to a single-use recovery code.
                digest = crypto.sha256_hex(crypto.normalize_recovery_code(code))
                ok = await RecoveryCodeRepo(db).consume(user_id, digest)
                if ok:
                    await audit.record(AuthEventType.RECOVERY_CODE_USED, user_id=user_id)
            if not ok:
                await audit.record(
                    AuthEventType.MFA_CHALLENGE_FAILURE,
                    user_id=user_id,
                    success=False,
                    ip_address=ctx.ip,
                )
                raise MfaInvalid()
            await AuthCredentialRepo(db).record_login_success(user_id)
            bundle = await self._establish_session(db, user, ctx)
            await audit.record(
                AuthEventType.MFA_CHALLENGE_SUCCESS,
                user_id=user_id,
                session_id=bundle.session_id,
                ip_address=ctx.ip,
            )
        return bundle

    # ------------------------------------------------------------------ #
    # Refresh-token rotation + reuse detection
    # ------------------------------------------------------------------ #

    async def refresh(self, refresh_token: str, *, ctx: LoginContext | None = None) -> TokenBundle:
        """Rotate a refresh token. A replayed token revokes its whole family.

        This is the heart of the breach-resilient scheme: every refresh consumes
        the presented token and issues a fresh one in the same family. Presenting
        a token that was already consumed (``used_at`` set) means it leaked and is
        being replayed — we revoke the entire family so neither the attacker nor
        the victim can keep using it (kinora.md §12).
        """
        ctx = ctx or LoginContext()
        digest = TokenService.refresh_digest(refresh_token)
        # Reuse detection must be DURABLE: revoking the family then raising in the
        # same unit of work would roll the revocation back. So the reuse path runs
        # (and commits) in its own transaction, and only then do we raise.
        if await self._handle_possible_reuse(digest, ctx):
            raise TokenReused()
        async with self._sf() as db:
            repo = RefreshTokenRepo(db)
            row = await repo.get_by_digest(digest)
            audit = AuthAuditRepo(db)
            if row is None or row.revoked_at is not None:
                raise TokenInvalid("refresh token unknown or revoked")
            if row.used_at is not None:
                # Raced with another refresh of the same token — treat as reuse.
                raise TokenReused()
            if row.expires_at <= datetime.now(UTC):
                raise TokenInvalid("refresh token expired")
            user = await UserRepo(db).get(row.user_id)
            if user is None:
                raise TokenInvalid("user no longer exists")
            creds = await AuthCredentialRepo(db).get(user.id)
            if creds is not None and creds.disabled:
                raise AccountDisabled()
            # Session must still be active for the refresh to stand.
            if row.session_id and not await AuthSessionRepo(db).is_active(row.session_id):
                raise TokenInvalid("session revoked")
            # Rotate: issue a child in the same family, consume the parent.
            issued = self._tokens.issue_refresh_token(family_id=row.family_id)
            child = await repo.create(
                user_id=user.id,
                token_digest=issued.digest,
                family_id=issued.family_id,
                expires_at=issued.expires_at,
                session_id=row.session_id,
            )
            await repo.mark_used(row, replaced_by=child.id)
            if row.session_id:
                await AuthSessionRepo(db).touch(row.session_id, ip=ctx.ip)
            access, _ = await self._mint_access(db, user, row.session_id)
            await audit.record(
                AuthEventType.TOKEN_REFRESH,
                user_id=user.id,
                session_id=row.session_id,
                ip_address=ctx.ip,
            )
            session_id = row.session_id
        return TokenBundle(
            access_token=access,
            refresh_token=issued.token,
            expires_in=self._settings.access_token_ttl_s,
            session_id=session_id or "",
        )

    async def _handle_possible_reuse(self, digest: str, ctx: LoginContext) -> bool:
        """If ``digest`` names a consumed token, burn its family in a committed tx.

        Returns ``True`` when reuse was detected (the caller then raises
        :class:`TokenReused`). Runs in its own unit of work so the revocation is
        durable even though the caller raises immediately afterwards.
        """
        async with self._sf() as db:
            repo = RefreshTokenRepo(db)
            row = await repo.get_by_digest(digest)
            if row is None or row.used_at is None:
                return False
            # A consumed token presented again is a replay: revoke the family +
            # its session so neither attacker nor victim can keep using it.
            await repo.revoke_family(row.family_id, reason="reuse_detected")
            if row.session_id:
                await AuthSessionRepo(db).revoke(row.session_id, reason="reuse_detected")
            await AuthAuditRepo(db).record(
                AuthEventType.TOKEN_REUSE,
                user_id=row.user_id,
                success=False,
                session_id=row.session_id,
                ip_address=ctx.ip,
            )
            logger.warning("auth.token_reuse", user_id=row.user_id, family=row.family_id)
        return True

    # ------------------------------------------------------------------ #
    # Logout
    # ------------------------------------------------------------------ #

    async def logout(self, claims_jti: str, *, session_id: str | None, user_id: str) -> None:
        """Revoke the caller's current session + its refresh tokens, deny the access jti."""
        async with self._sf() as db:
            if session_id:
                await AuthSessionRepo(db).revoke(session_id, reason="logout")
                await RefreshTokenRepo(db).revoke_for_session(session_id)
            await AuthAuditRepo(db).record(
                AuthEventType.LOGOUT, user_id=user_id, session_id=session_id
            )
        # The access token is stateless; deny its jti for the rest of its life.
        if claims_jti:
            await self._revocations.revoke(claims_jti, ttl_s=self._settings.access_token_ttl_s)

    async def logout_all(self, user_id: str, *, keep_session: str | None = None) -> int:
        """Revoke every session + refresh token for a user (security panic button)."""
        async with self._sf() as db:
            revoked = await AuthSessionRepo(db).revoke_all(
                user_id, reason="logout_all", keep=keep_session
            )
            await RefreshTokenRepo(db).revoke_for_user(user_id)
            await AuthAuditRepo(db).record(AuthEventType.LOGOUT_ALL, user_id=user_id)
        return revoked

    # ------------------------------------------------------------------ #
    # Password change / reset
    # ------------------------------------------------------------------ #

    async def change_password(self, user_id: str, current: str, new: str) -> None:
        """Change a password (verifying the current one) and revoke other sessions."""
        problems = self._policy.validate(new)
        if problems:
            raise WeakPassword(problems)
        async with self._sf() as db:
            users = UserRepo(db)
            user = await users.get(user_id)
            if user is None or not self._hasher.verify(current, user.hashed_password):
                raise InvalidCredentials()
            await users.set_password(user_id, self._hasher.hash(new))
            creds = AuthCredentialRepo(db)
            await creds.mark_password_changed(user_id, scheme=self._hasher.scheme)
            # A password change invalidates every refresh token (force re-login).
            await RefreshTokenRepo(db).revoke_for_user(user_id)
            await AuthSessionRepo(db).revoke_all(user_id, reason="password_change")
            await AuthAuditRepo(db).record(AuthEventType.PASSWORD_CHANGE, user_id=user_id)

    async def reset_password_with_token(self, reset_token: str, new: str) -> None:
        """Reset a password using a one-time reset token (decoded as an MFA-style jwt)."""
        problems = self._policy.validate(new)
        if problems:
            raise WeakPassword(problems)
        # Reuse the MFA-challenge decoder shape for the reset token (same crypto).
        user_id = self._tokens.decode_mfa_challenge(reset_token)
        async with self._sf() as db:
            users = UserRepo(db)
            if await users.get(user_id) is None:
                raise InvalidCredentials()
            await users.set_password(user_id, self._hasher.hash(new))
            await AuthCredentialRepo(db).mark_password_changed(user_id, scheme=self._hasher.scheme)
            await RefreshTokenRepo(db).revoke_for_user(user_id)
            await AuthSessionRepo(db).revoke_all(user_id, reason="password_reset")
            await AuthAuditRepo(db).record(AuthEventType.PASSWORD_RESET, user_id=user_id)

    async def issue_password_reset_token(self, email: str) -> str | None:
        """Mint a one-time password-reset token (``None`` for an unknown email).

        The caller (a route or a mailer) decides how to deliver it; returning
        ``None`` for unknown emails avoids account enumeration on the endpoint.
        """
        async with self._sf() as db:
            user = await UserRepo(db).get_by_email(email)
            if user is None:
                return None
            await AuthAuditRepo(db).record(AuthEventType.PASSWORD_RESET_REQUEST, user_id=user.id)
        return self._tokens.issue_mfa_challenge(user.id)

    # ------------------------------------------------------------------ #
    # MFA enrolment / disable
    # ------------------------------------------------------------------ #

    async def begin_mfa_enrollment(self, user_id: str) -> MfaEnrollment:
        """Generate a TOTP secret + recovery codes; MFA stays *pending* until verified."""
        async with self._sf() as db:
            user = await UserRepo(db).get(user_id)
            if user is None:
                raise InvalidCredentials()
            creds = await AuthCredentialRepo(db).get_or_create(user_id)
            if creds.mfa_enabled:
                raise MfaAlreadyEnrolled()
            secret = crypto.generate_totp_secret()
            codes = crypto.generate_recovery_codes(self._settings.recovery_code_count)
            # Store the secret but leave mfa_enabled False until the user proves a code.
            await AuthCredentialRepo(db).set_mfa(
                user_id, secret=secret, enabled=False, method=MfaMethod.TOTP
            )
            await RecoveryCodeRepo(db).replace_all(user_id, [c.digest for c in codes])
            await AuthAuditRepo(db).record(AuthEventType.MFA_ENROLL, user_id=user_id)
            uri = crypto.totp_provisioning_uri(
                secret, account=user.email, issuer=self._settings.mfa_issuer
            )
        return MfaEnrollment(
            secret=secret, provisioning_uri=uri, recovery_codes=[c.plaintext for c in codes]
        )

    async def confirm_mfa_enrollment(self, user_id: str, code: str) -> None:
        """Activate MFA after the user proves a valid TOTP code from their app."""
        async with self._sf() as db:
            creds = await AuthCredentialRepo(db).get(user_id)
            if creds is None or not creds.totp_secret:
                raise MfaNotEnrolled()
            if creds.mfa_enabled:
                raise MfaAlreadyEnrolled()
            if not crypto.verify_totp(
                creds.totp_secret, code, window=self._settings.totp_drift_window
            ):
                raise MfaInvalid()
            await AuthCredentialRepo(db).set_mfa(
                user_id, secret=creds.totp_secret, enabled=True, method=MfaMethod.TOTP
            )
            await AuthAuditRepo(db).record(AuthEventType.MFA_ENABLE, user_id=user_id)

    async def disable_mfa(self, user_id: str, code: str) -> None:
        """Disable MFA (requires a valid current TOTP/recovery code)."""
        async with self._sf() as db:
            creds = await AuthCredentialRepo(db).get(user_id)
            if creds is None or not creds.mfa_enabled or not creds.totp_secret:
                raise MfaNotEnrolled()
            ok = crypto.verify_totp(
                creds.totp_secret, code, window=self._settings.totp_drift_window
            )
            if not ok:
                digest = crypto.sha256_hex(crypto.normalize_recovery_code(code))
                ok = await RecoveryCodeRepo(db).consume(user_id, digest)
            if not ok:
                raise MfaInvalid()
            await AuthCredentialRepo(db).set_mfa(user_id, secret=None, enabled=False, method=None)
            await RecoveryCodeRepo(db).replace_all(user_id, [])
            await AuthAuditRepo(db).record(AuthEventType.MFA_DISABLE, user_id=user_id)

    async def regenerate_recovery_codes(self, user_id: str) -> list[str]:
        """Replace a user's recovery codes (MFA must already be enabled)."""
        async with self._sf() as db:
            creds = await AuthCredentialRepo(db).get(user_id)
            if creds is None or not creds.mfa_enabled:
                raise MfaNotEnrolled()
            codes = crypto.generate_recovery_codes(self._settings.recovery_code_count)
            await RecoveryCodeRepo(db).replace_all(user_id, [c.digest for c in codes])
            await AuthAuditRepo(db).record(
                AuthEventType.RECOVERY_CODES_REGENERATED, user_id=user_id
            )
        return [c.plaintext for c in codes]

    # ------------------------------------------------------------------ #
    # API keys
    # ------------------------------------------------------------------ #

    async def create_api_key(
        self,
        user_id: str,
        *,
        name: str,
        scopes: Sequence[str],
        expires_in_s: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Issue an API key. Returns ``(plaintext_secret, public_metadata)``.

        The plaintext is shown exactly once; only the peppered HMAC digest is
        stored. Scopes are validated against the catalogue and may not exceed the
        owner's effective permissions (an editor cannot mint an admin key).
        """
        requested = normalize_scopes(scopes)
        async with self._sf() as db:
            principal = await self._build_user_principal(db, user_id)
            granted = [s for s in requested if principal.has_permission(s)]
            issued, digest = crypto.generate_api_key(pepper=self._settings.api_key_pepper)
            expires_at = (
                datetime.now(UTC) + timedelta(seconds=expires_in_s)
                if expires_in_s
                else (
                    datetime.now(UTC) + timedelta(seconds=self._settings.api_key_default_ttl_s)
                    if self._settings.api_key_default_ttl_s
                    else None
                )
            )
            row = await ApiKeyRepo(db).create(
                user_id=user_id,
                key_id=issued.key_id,
                secret_digest=digest,
                name=name,
                scopes=granted,
                display_prefix=issued.display_prefix,
                tenant_id=principal.tenant_id,
                expires_at=expires_at,
            )
            await AuthAuditRepo(db).record(
                AuthEventType.API_KEY_CREATE,
                user_id=user_id,
                detail={"key_id": issued.key_id, "scopes": granted},
            )
            meta = _api_key_public(row)
        return issued.secret, meta

    async def list_api_keys(self, user_id: str) -> list[dict[str, Any]]:
        """List a user's API keys (public metadata only; never the secret)."""
        async with self._sf() as db:
            rows = await ApiKeyRepo(db).list_for_user(user_id)
            return [_api_key_public(r) for r in rows]

    async def revoke_api_key(self, user_id: str, api_key_id: str) -> None:
        """Revoke one of the caller's API keys."""
        async with self._sf() as db:
            ok = await ApiKeyRepo(db).revoke(api_key_id, user_id=user_id)
            if not ok:
                raise ApiKeyInvalid("api key not found")
            await AuthAuditRepo(db).record(
                AuthEventType.API_KEY_REVOKE, user_id=user_id, detail={"api_key_id": api_key_id}
            )

    async def authenticate_api_key(self, presented: str) -> Principal:
        """Verify a presented API key and build its :class:`Principal`."""
        parsed = crypto.parse_api_key(presented)
        if parsed is None:
            raise ApiKeyInvalid("malformed api key")
        key_id, secret_part = parsed
        async with self._sf() as db:
            row = await ApiKeyRepo(db).get_by_key_id(key_id)
            if row is None or row.revoked_at is not None:
                raise ApiKeyInvalid("unknown or revoked key")
            if row.expires_at is not None and row.expires_at <= datetime.now(UTC):
                raise ApiKeyInvalid("expired key")
            if not crypto.verify_api_key(
                secret_part, row.secret_digest, pepper=self._settings.api_key_pepper
            ):
                raise ApiKeyInvalid("bad key secret")
            creds = await AuthCredentialRepo(db).get(row.user_id)
            if creds is not None and creds.disabled:
                raise AccountDisabled()
            await ApiKeyRepo(db).touch(row.id)
            await AuthAuditRepo(db).record(
                AuthEventType.API_KEY_USED, user_id=row.user_id, detail={"key_id": key_id}
            )
            return Principal(
                user_id=row.user_id,
                permissions=frozenset(row.scopes),
                roles=frozenset(),
                tenant_id=row.tenant_id,
                api_key_id=row.id,
            )

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #

    async def list_sessions(self, user_id: str) -> list[dict[str, Any]]:
        """List the caller's active sessions (the "active devices" view)."""
        async with self._sf() as db:
            rows = await AuthSessionRepo(db).list_active(user_id)
            return [_session_public(r) for r in rows]

    async def revoke_session(self, user_id: str, session_id: str) -> None:
        """Revoke one of the caller's own sessions + its refresh tokens."""
        async with self._sf() as db:
            row = await AuthSessionRepo(db).get(session_id)
            if row is None or row.user_id != user_id:
                raise SessionNotFound()
            await AuthSessionRepo(db).revoke(session_id, reason="revoked")
            await RefreshTokenRepo(db).revoke_for_session(session_id)
            await AuthAuditRepo(db).record(
                AuthEventType.SESSION_REVOKE, user_id=user_id, session_id=session_id
            )

    # ------------------------------------------------------------------ #
    # RBAC admin + principal building
    # ------------------------------------------------------------------ #

    async def grant_role(
        self,
        *,
        user_id: str,
        role: str,
        tenant_id: str | None = None,
        granted_by: str | None = None,
    ) -> None:
        """Grant a role to a user (admin action)."""
        async with self._sf() as db:
            await self._ensure_catalogue(db)
            rbac = RbacRepo(db)
            role_row = await rbac.get_role(role)
            if role_row is None:
                raise PermissionDenied(f"unknown role {role!r}")
            await rbac.bind_role(
                user_id=user_id, role_id=role_row.id, tenant_id=tenant_id, granted_by=granted_by
            )
            await AuthAuditRepo(db).record(
                AuthEventType.ROLE_GRANT,
                user_id=user_id,
                detail={"role": role, "tenant_id": tenant_id, "by": granted_by},
            )

    async def revoke_role(self, *, user_id: str, role: str, tenant_id: str | None = None) -> None:
        """Revoke a role from a user (admin action)."""
        async with self._sf() as db:
            rbac = RbacRepo(db)
            role_row = await rbac.get_role(role)
            if role_row is None:
                raise PermissionDenied(f"unknown role {role!r}")
            await rbac.unbind_role(user_id=user_id, role_id=role_row.id, tenant_id=tenant_id)
            await AuthAuditRepo(db).record(
                AuthEventType.ROLE_REVOKE, user_id=user_id, detail={"role": role}
            )

    async def set_account_disabled(self, user_id: str, disabled: bool) -> None:
        """Enable/disable an account (admin action) + log it."""
        async with self._sf() as db:
            await AuthCredentialRepo(db).set_disabled(user_id, disabled)
            if disabled:
                await RefreshTokenRepo(db).revoke_for_user(user_id)
                await AuthSessionRepo(db).revoke_all(user_id, reason="account_disabled")
            await AuthAuditRepo(db).record(
                AuthEventType.ACCOUNT_DISABLED if disabled else AuthEventType.ACCOUNT_ENABLED,
                user_id=user_id,
            )

    async def run_retention_sweep(self) -> dict[str, int]:
        """Housekeeping: purge expired refresh tokens + aged audit rows.

        Safe to call periodically (e.g. from a scheduled task). Returns how many
        rows were removed in each category. ``auth_audit_retention_days`` of 0
        disables audit pruning.
        """
        removed = {"refresh_tokens": 0, "audit_rows": 0}
        async with self._sf() as db:
            removed["refresh_tokens"] = await RefreshTokenRepo(db).purge_expired()
            days = self._settings.auth_audit_retention_days
            if days > 0:
                cutoff = datetime.now(UTC) - timedelta(days=days)
                removed["audit_rows"] = await AuthAuditRepo(db).purge_older_than(cutoff)
        logger.info("auth.retention_sweep", **removed)
        return removed

    async def build_principal_for_user(
        self, user_id: str, *, session_id: str | None = None
    ) -> Principal:
        """Build a fresh :class:`Principal` for a user from the DB (route dep helper)."""
        async with self._sf() as db:
            principal = await self._build_user_principal(db, user_id)
        if session_id is not None:
            principal = Principal(
                user_id=principal.user_id,
                permissions=principal.permissions,
                roles=principal.roles,
                tenant_id=principal.tenant_id,
                session_id=session_id,
            )
        return principal

    async def read_audit_log(self, user_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Recent audit events for a user (the "security activity" view)."""
        async with self._sf() as db:
            rows = await AuthAuditRepo(db).list_for_user(user_id, limit=limit)
            return [_audit_public(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _guard_ip_throttle(self, ip: str | None) -> None:
        if not ip:
            return
        count = await self._throttle.hit(ip)
        if count > self._settings.login_ip_max_attempts:
            retry = await self._throttle.retry_after_s(ip)
            raise AccountLocked("too many login attempts from this network", retry_after_s=retry)

    async def _record_login_failure(
        self,
        user_id: str | None,
        ctx: LoginContext,
        *,
        reason: str,
        lockout: bool = True,
    ) -> None:
        """Persist a login failure (+ optionally bump the lockout counter) durably.

        Runs in its own committed unit of work so the caller can ``raise``
        immediately afterwards without rolling the counter increment back.
        """
        async with self._sf() as db:
            if user_id is not None and lockout:
                await AuthCredentialRepo(db).record_login_failure(
                    user_id,
                    max_failures=self._settings.login_max_failures,
                    lockout_s=self._settings.login_lockout_duration_s,
                )
            await AuthAuditRepo(db).record(
                AuthEventType.LOGIN_FAILURE,
                user_id=user_id,
                success=False,
                ip_address=ctx.ip,
                user_agent=ctx.user_agent,
                detail={"reason": reason},
            )

    async def _audit(
        self,
        event: AuthEventType,
        *,
        user_id: str | None,
        ctx: LoginContext,
        success: bool = True,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Append one audit record in its own committed transaction."""
        async with self._sf() as db:
            await AuthAuditRepo(db).record(
                event,
                user_id=user_id,
                success=success,
                ip_address=ctx.ip,
                user_agent=ctx.user_agent,
                detail=detail,
            )

    async def _establish_session(
        self, db: AsyncSession, user: User, ctx: LoginContext
    ) -> TokenBundle:
        """Create a session (enforcing the per-user cap) + a fresh token family."""
        sessions = AuthSessionRepo(db)
        # Enforce the concurrent-session cap (evict the oldest).
        if await sessions.count_active(user.id) >= self._settings.max_sessions_per_user:
            oldest = await sessions.oldest_active(user.id)
            if oldest is not None:
                await sessions.revoke(oldest.id, reason="session_cap")
                await RefreshTokenRepo(db).revoke_for_session(oldest.id)
        device: DeviceInfo = crypto.parse_device(ctx.user_agent, ctx.ip)
        expires_at = datetime.now(UTC) + timedelta(seconds=self._settings.refresh_token_ttl_s)
        session = await sessions.create(
            user_id=user.id,
            device_label=device.label,
            user_agent=ctx.user_agent,
            ip_address=ctx.ip,
            fingerprint=device.fingerprint,
            expires_at=expires_at,
        )
        issued: IssuedRefreshToken = self._tokens.issue_refresh_token()
        await RefreshTokenRepo(db).create(
            user_id=user.id,
            token_digest=issued.digest,
            family_id=issued.family_id,
            expires_at=issued.expires_at,
            session_id=session.id,
        )
        access, _ = await self._mint_access(db, user, session.id)
        return TokenBundle(
            access_token=access,
            refresh_token=issued.token,
            expires_in=self._settings.access_token_ttl_s,
            session_id=session.id,
        )

    async def _mint_access(
        self, db: AsyncSession, user: User, session_id: str | None
    ) -> tuple[str, Any]:
        principal = await self._build_user_principal(db, user.id)
        return self._tokens.issue_access_token(
            user.id,
            session_id=session_id,
            roles=sorted(principal.roles),
            scopes=sorted(principal.permissions),
            tenant=principal.tenant_id,
        )

    async def _build_user_principal(self, db: AsyncSession, user_id: str) -> Principal:
        rbac = RbacRepo(db)
        roles = await rbac.roles_for_user(user_id)
        role_names = {r for r, _ in roles}
        tenants = {t for _, t in roles if t is not None}
        # Permissions: prefer DB role_permissions, falling back to the built-in
        # catalogue for system roles whose perms may not be seeded yet.
        perms = await rbac.permissions_for_user(user_id)
        perms |= expand_roles_to_permissions(role_names)
        creds = await AuthCredentialRepo(db).get(user_id)
        tenant = creds.tenant_id if creds and creds.tenant_id else next(iter(tenants), None)
        return Principal(
            user_id=user_id,
            permissions=frozenset(perms),
            roles=frozenset(role_names),
            tenant_id=tenant,
        )

    async def _ensure_catalogue(self, db: AsyncSession) -> None:
        """Idempotently seed the built-in permissions + roles + their links."""
        rbac = RbacRepo(db)
        for name, desc in PERMISSIONS.items():
            await rbac.upsert_permission(name, description=desc)
        for role_name, perm_names in ROLES.items():
            role = await rbac.upsert_role(role_name, is_system=role_name in SYSTEM_ROLES)
            for perm_name in perm_names:
                if perm_name == "*":
                    continue  # the wildcard is matched in code, not stored as a perm
                perm = await rbac.get_permission(perm_name)
                if perm is not None:
                    await rbac.add_permission_to_role(role.id, perm.id)


# --------------------------------------------------------------------------- #
# Module helpers
# --------------------------------------------------------------------------- #

#: A throwaway bcrypt hash used to equalise timing on an unknown-email login.
_DUMMY_HASH = crypto.BcryptHasher(rounds=4).hash("kinora-dummy-equalise-timing")


def _policy_from_settings(settings: Settings) -> PasswordPolicy:
    return PasswordPolicy(
        min_length=settings.password_min_length,
        require_lower=settings.password_require_lower,
        require_upper=settings.password_require_upper,
        require_digit=settings.password_require_digit,
        require_symbol=settings.password_require_symbol,
        block_common=settings.password_block_common,
    )


def _is_locked(locked_until: datetime | None) -> bool:
    return locked_until is not None and locked_until > datetime.now(UTC)


def _seconds_until(when: datetime | None) -> int:
    if when is None:
        return 0
    return max(int((when - datetime.now(UTC)).total_seconds()), 0)


def _api_key_public(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "key_id": row.key_id,
        "display_prefix": row.display_prefix,
        "scopes": list(row.scopes or []),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked": row.revoked_at is not None,
    }


def _session_public(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "device_label": row.device_label,
        "ip_address": row.ip_address,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


def _audit_public(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "event": row.event.value if hasattr(row.event, "value") else str(row.event),
        "success": row.success,
        "ip_address": row.ip_address,
        "session_id": row.session_id,
        "detail": row.detail,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


__all__ = [
    "AuthService",
    "LoginContext",
    "MfaEnrollment",
    "TokenBundle",
]
