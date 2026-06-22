"""Auth routes — register, login, me (kinora.md §6 security).

Real JWT auth over the ``users`` table: register hashes the password with bcrypt
and inserts the user; login verifies and issues a signed access token; ``/auth/me``
echoes the authenticated user. The auth surface is rate-limited (credential
stuffing defence).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.api.deps import ContainerDep, CurrentUser, auth_rate_limit
from app.api.errors import APIError
from app.api.schemas import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from app.api.security import create_access_token, hash_password, verify_password
from app.core.logging import get_logger
from app.db.models.user import User
from app.db.repositories.user import UserRepo

logger = get_logger("app.api.auth")

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(auth_rate_limit)])


def _user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        created_at=user.created_at.isoformat() if user.created_at else None,
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, container: ContainerDep) -> UserResponse:
    """Create an account (email + bcrypt-hashed password)."""
    async with container.session_factory() as session:
        repo = UserRepo(session)
        if await repo.get_by_email(body.email) is not None:
            raise APIError("email_taken", "an account with this email already exists", status=409)
        user = await repo.create(email=body.email, hashed_password=hash_password(body.password))
        response = _user_response(user)
    logger.info("auth.registered", user_id=response.id)
    return response


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, container: ContainerDep) -> TokenResponse:
    """Verify credentials and issue a signed access token."""
    async with container.session_factory() as session:
        user = await UserRepo(session).get_by_email(body.email)
    if user is None or not verify_password(body.password, user.hashed_password):
        raise APIError("invalid_credentials", "incorrect email or password", status=401)
    token = create_access_token(user.id, container.settings)
    logger.info("auth.login", user_id=user.id)
    return TokenResponse(access_token=token, expires_in=container.settings.access_token_ttl_s)


@router.get("/me", response_model=UserResponse)
async def me(user: CurrentUser) -> UserResponse:
    """Return the authenticated user."""
    return _user_response(user)


__all__ = ["router"]
