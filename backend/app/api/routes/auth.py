"""Auth routes — the production auth & security surface (kinora.md §6, §12).

Built on :class:`app.auth.service.AuthService`, this router keeps the original
``/register`` / ``/login`` / ``/me`` contract (so the desktop client and the
existing tests are unaffected) and adds the full production surface:

* **session lifecycle** — refresh (rotation + reuse detection), logout,
  logout-all, list/revoke sessions;
* **password** — change, request-reset, reset;
* **MFA** — enrol, confirm, login-completion, disable, regenerate recovery codes;
* **API keys** — create (scoped), list, revoke;
* **RBAC admin** — grant/revoke roles, list catalogue, disable/enable accounts;
* **security activity** — the audit log.

Every endpoint is rate-limited (the existing ``auth_rate_limit`` bucket) and the
auth-domain :class:`~app.auth.errors.AuthError` is translated to the gateway's
typed :class:`~app.api.errors.APIError` envelope by :func:`_translate`.
"""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import ContainerDep, CurrentUser, auth_rate_limit
from app.api.errors import APIError
from app.api.schemas import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from app.auth.deps import CurrentPrincipal, LoginCtx, require_admin
from app.auth.errors import AuthError, MfaRequired
from app.auth.rbac import PERMISSIONS, ROLES, Principal
from app.auth.service import AuthService, TokenBundle
from app.core.logging import get_logger
from app.core.security import password_entropy_bits
from app.db.models.user import User

logger = get_logger("app.api.auth")

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(auth_rate_limit)])

T = TypeVar("T")


async def _translate(coro: Coroutine[Any, Any, T]) -> T:
    """Await ``coro``, mapping any :class:`AuthError` onto an :class:`APIError`."""
    try:
        return await coro
    except AuthError as exc:
        detail: dict[str, Any] | None = None
        retry = getattr(exc, "retry_after_s", None)
        if retry:
            detail = {"retry_after_s": retry}
        if hasattr(exc, "problems"):
            detail = {"problems": exc.problems}
        raise APIError(exc.code, exc.message, status=exc.status, detail=detail) from exc


def _service(container: ContainerDep) -> AuthService:
    return container.auth_service


# --------------------------------------------------------------------------- #
# Request/response models (auth-specific; the shared ones live in api.schemas)
# --------------------------------------------------------------------------- #


class LoginResponse(BaseModel):
    """A login result: tokens, or an ``mfa_required`` challenge."""

    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int | None = None
    session_id: str | None = None
    mfa_required: bool = False
    mfa_token: str | None = None


class RefreshRequest(BaseModel):
    """Exchange a refresh token for a rotated pair."""

    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=8, max_length=512)


class MfaLoginRequest(BaseModel):
    """Complete a 2FA login with the challenge token + a TOTP/recovery code."""

    model_config = ConfigDict(extra="forbid")

    mfa_token: str = Field(min_length=8, max_length=1024)
    code: str = Field(min_length=4, max_length=40)


class ChangePasswordRequest(BaseModel):
    """Change the caller's password."""

    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


class PasswordResetRequest(BaseModel):
    """Request a password-reset token for an email (always 202, no enumeration)."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)


class PasswordResetConfirm(BaseModel):
    """Reset a password with a one-time reset token."""

    model_config = ConfigDict(extra="forbid")

    reset_token: str = Field(min_length=8, max_length=1024)
    new_password: str = Field(min_length=8, max_length=200)


class MfaCodeRequest(BaseModel):
    """A TOTP/recovery code (MFA confirm / disable)."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=4, max_length=40)


class MfaEnrollResponse(BaseModel):
    """The TOTP secret + provisioning URI + one-time recovery codes."""

    secret: str
    provisioning_uri: str
    recovery_codes: list[str]


class CreateApiKeyRequest(BaseModel):
    """Create a scoped API key."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=list)
    expires_in_s: int | None = Field(default=None, ge=60)


class CreateApiKeyResponse(BaseModel):
    """A freshly-created API key (the secret is shown exactly once)."""

    secret: str
    api_key: dict[str, Any]


class GrantRoleRequest(BaseModel):
    """Grant a role to a user (admin)."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)
    role: str = Field(min_length=1, max_length=80)
    tenant_id: str | None = Field(default=None, max_length=64)


class SetDisabledRequest(BaseModel):
    """Enable/disable an account (admin)."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)
    disabled: bool


def _user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        created_at=user.created_at.isoformat() if user.created_at else None,
    )


def _bundle_response(bundle: TokenBundle) -> LoginResponse:
    return LoginResponse(
        access_token=bundle.access_token,
        refresh_token=bundle.refresh_token,
        token_type=bundle.token_type,
        expires_in=bundle.expires_in,
        session_id=bundle.session_id,
    )


# --------------------------------------------------------------------------- #
# Registration + login + me
# --------------------------------------------------------------------------- #


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, container: ContainerDep, ctx: LoginCtx) -> UserResponse:
    """Create an account (email + hashed password), assigning the default role."""
    user = await _translate(_service(container).register(body.email, body.password, ctx=ctx))
    return _user_response(user)


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, container: ContainerDep, ctx: LoginCtx) -> LoginResponse:
    """Verify credentials and issue an access+refresh pair (or an MFA challenge)."""
    try:
        result = await _service(container).login(body.email, body.password, ctx=ctx)
    except MfaRequired as exc:
        return LoginResponse(mfa_required=True, mfa_token=exc.mfa_token)
    except AuthError as exc:
        retry = getattr(exc, "retry_after_s", None)
        detail = {"retry_after_s": retry} if retry else None
        raise APIError(exc.code, exc.message, status=exc.status, detail=detail) from exc
    # ``login`` returns a TokenBundle on the no-MFA path.
    assert isinstance(result, TokenBundle)
    return _bundle_response(result)


@router.post("/login/legacy", response_model=TokenResponse)
async def login_legacy(body: LoginRequest, container: ContainerDep, ctx: LoginCtx) -> TokenResponse:
    """Legacy login shape: a bare access token (no refresh), for old clients."""
    try:
        result = await _service(container).login(body.email, body.password, ctx=ctx)
    except MfaRequired as exc:
        raise APIError("mfa_required", "second factor required", status=401) from exc
    except AuthError as exc:
        raise APIError(exc.code, exc.message, status=exc.status) from exc
    assert isinstance(result, TokenBundle)
    return TokenResponse(access_token=result.access_token, expires_in=result.expires_in)


@router.post("/mfa/login", response_model=LoginResponse)
async def mfa_login(body: MfaLoginRequest, container: ContainerDep, ctx: LoginCtx) -> LoginResponse:
    """Complete a 2FA login: exchange the challenge token + code for a session."""
    bundle = await _translate(
        _service(container).complete_mfa_login(body.mfa_token, body.code, ctx=ctx)
    )
    return _bundle_response(bundle)


@router.get("/me", response_model=UserResponse)
async def me(user: CurrentUser) -> UserResponse:
    """Return the authenticated user (legacy Bearer flow; unchanged)."""
    return _user_response(user)


@router.get("/whoami")
async def whoami(principal: CurrentPrincipal) -> dict[str, Any]:
    """Return the caller's effective authorisation context (roles/scopes/tenant)."""
    return {
        "user_id": principal.user_id,
        "roles": sorted(principal.roles),
        "permissions": sorted(principal.permissions),
        "tenant_id": principal.tenant_id,
        "session_id": principal.session_id,
        "is_api_key": principal.is_api_key,
    }


# --------------------------------------------------------------------------- #
# Token lifecycle
# --------------------------------------------------------------------------- #


@router.post("/refresh", response_model=LoginResponse)
async def refresh(body: RefreshRequest, container: ContainerDep, ctx: LoginCtx) -> LoginResponse:
    """Rotate a refresh token (replay of a consumed token revokes the family)."""
    bundle = await _translate(_service(container).refresh(body.refresh_token, ctx=ctx))
    return _bundle_response(bundle)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(principal: CurrentPrincipal, container: ContainerDep) -> None:
    """Revoke the caller's current session + its refresh tokens."""
    await _service(container).logout("", session_id=principal.session_id, user_id=principal.user_id)


@router.post("/logout-all")
async def logout_all(principal: CurrentPrincipal, container: ContainerDep) -> dict[str, int]:
    """Revoke every session for the caller (security panic button)."""
    revoked = await _service(container).logout_all(principal.user_id)
    return {"revoked_sessions": revoked}


# --------------------------------------------------------------------------- #
# Password
# --------------------------------------------------------------------------- #


@router.post("/password/change", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest, principal: CurrentPrincipal, container: ContainerDep
) -> None:
    """Change the caller's password (revokes all other sessions)."""
    await _translate(
        _service(container).change_password(
            principal.user_id, body.current_password, body.new_password
        )
    )


@router.post("/password/reset-request", status_code=status.HTTP_202_ACCEPTED)
async def password_reset_request(
    body: PasswordResetRequest, container: ContainerDep
) -> dict[str, Any]:
    """Request a password-reset token (always 202; the token is logged/emailed).

    In production the token is delivered out-of-band; here we return it only in
    the local environment so the desktop dev flow can complete a reset.
    """
    token = await _service(container).issue_password_reset_token(body.email)
    payload: dict[str, Any] = {"status": "accepted"}
    if token is not None and container.settings.is_local:
        payload["reset_token"] = token
    return payload


@router.post("/password/reset", status_code=status.HTTP_204_NO_CONTENT)
async def password_reset(body: PasswordResetConfirm, container: ContainerDep) -> None:
    """Reset a password using a one-time reset token."""
    await _translate(
        _service(container).reset_password_with_token(body.reset_token, body.new_password)
    )


@router.get("/password/strength")
async def password_strength(password: str) -> dict[str, Any]:
    """Advisory password-strength check (entropy estimate + policy verdict)."""
    return {"entropy_bits": password_entropy_bits(password)}


# --------------------------------------------------------------------------- #
# MFA
# --------------------------------------------------------------------------- #


@router.post("/mfa/enroll", response_model=MfaEnrollResponse)
async def mfa_enroll(principal: CurrentPrincipal, container: ContainerDep) -> MfaEnrollResponse:
    """Begin TOTP enrolment: returns the secret, QR URI, and one-time recovery codes."""
    enrollment = await _translate(_service(container).begin_mfa_enrollment(principal.user_id))
    return MfaEnrollResponse(
        secret=enrollment.secret,
        provisioning_uri=enrollment.provisioning_uri,
        recovery_codes=enrollment.recovery_codes,
    )


@router.post("/mfa/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def mfa_confirm(
    body: MfaCodeRequest, principal: CurrentPrincipal, container: ContainerDep
) -> None:
    """Activate MFA after proving a TOTP code from the authenticator app."""
    await _translate(_service(container).confirm_mfa_enrollment(principal.user_id, body.code))


@router.post("/mfa/disable", status_code=status.HTTP_204_NO_CONTENT)
async def mfa_disable(
    body: MfaCodeRequest, principal: CurrentPrincipal, container: ContainerDep
) -> None:
    """Disable MFA (requires a valid current TOTP/recovery code)."""
    await _translate(_service(container).disable_mfa(principal.user_id, body.code))


@router.post("/mfa/recovery-codes")
async def regenerate_recovery_codes(
    principal: CurrentPrincipal, container: ContainerDep
) -> dict[str, list[str]]:
    """Replace the caller's recovery codes (MFA must be enabled)."""
    codes = await _translate(_service(container).regenerate_recovery_codes(principal.user_id))
    return {"recovery_codes": codes}


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #


@router.post("/api-keys", response_model=CreateApiKeyResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: CreateApiKeyRequest, principal: CurrentPrincipal, container: ContainerDep
) -> CreateApiKeyResponse:
    """Create a scoped API key (scopes capped to the caller's own permissions)."""
    secret, meta = await _translate(
        _service(container).create_api_key(
            principal.user_id,
            name=body.name,
            scopes=body.scopes,
            expires_in_s=body.expires_in_s,
        )
    )
    return CreateApiKeyResponse(secret=secret, api_key=meta)


@router.get("/api-keys")
async def list_api_keys(
    principal: CurrentPrincipal, container: ContainerDep
) -> dict[str, list[dict[str, Any]]]:
    """List the caller's API keys (never the secret)."""
    keys = await _service(container).list_api_keys(principal.user_id)
    return {"api_keys": keys}


@router.delete("/api-keys/{api_key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    api_key_id: str, principal: CurrentPrincipal, container: ContainerDep
) -> None:
    """Revoke one of the caller's API keys."""
    await _translate(_service(container).revoke_api_key(principal.user_id, api_key_id))


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


@router.get("/sessions")
async def list_sessions(
    principal: CurrentPrincipal, container: ContainerDep
) -> dict[str, list[dict[str, Any]]]:
    """List the caller's active sessions (the "active devices" view)."""
    sessions = await _service(container).list_sessions(principal.user_id)
    return {"sessions": sessions}


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: str, principal: CurrentPrincipal, container: ContainerDep
) -> None:
    """Revoke one of the caller's own sessions."""
    await _translate(_service(container).revoke_session(principal.user_id, session_id))


# --------------------------------------------------------------------------- #
# Security activity (audit)
# --------------------------------------------------------------------------- #


@router.get("/audit")
async def my_audit_log(
    principal: CurrentPrincipal, container: ContainerDep, limit: int = 50
) -> dict[str, list[dict[str, Any]]]:
    """Recent security events on the caller's own account."""
    events = await _service(container).read_audit_log(principal.user_id, limit=min(limit, 200))
    return {"events": events}


# --------------------------------------------------------------------------- #
# RBAC admin (gated on admin permissions)
# --------------------------------------------------------------------------- #


@router.get("/rbac/catalogue", dependencies=[Depends(require_admin)])
async def rbac_catalogue() -> dict[str, Any]:
    """The built-in permission + role catalogue (admin view)."""
    return {"permissions": PERMISSIONS, "roles": ROLES}


@router.post("/rbac/grant", status_code=status.HTTP_204_NO_CONTENT)
async def grant_role(
    body: GrantRoleRequest,
    container: ContainerDep,
    admin: Principal = Depends(require_admin),
) -> None:
    """Grant a role to a user (admin)."""
    await _translate(
        _service(container).grant_role(
            user_id=body.user_id,
            role=body.role,
            tenant_id=body.tenant_id,
            granted_by=admin.user_id,
        )
    )


@router.post("/rbac/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_role(
    body: GrantRoleRequest,
    container: ContainerDep,
    admin: Principal = Depends(require_admin),
) -> None:
    """Revoke a role from a user (admin)."""
    await _translate(
        _service(container).revoke_role(
            user_id=body.user_id, role=body.role, tenant_id=body.tenant_id
        )
    )


@router.post("/admin/account-disabled", status_code=status.HTTP_204_NO_CONTENT)
async def set_account_disabled(
    body: SetDisabledRequest,
    container: ContainerDep,
    admin: Principal = Depends(require_admin),
) -> None:
    """Enable/disable an account (admin); disabling also kills its sessions."""
    await _translate(_service(container).set_account_disabled(body.user_id, body.disabled))


@router.get("/admin/audit/{user_id}", dependencies=[Depends(require_admin)])
async def admin_audit_log(
    user_id: str, container: ContainerDep, limit: int = 100
) -> dict[str, list[dict[str, Any]]]:
    """Read any user's security audit log (admin)."""
    events = await _service(container).read_audit_log(user_id, limit=min(limit, 500))
    return {"events": events}


__all__ = ["router"]
