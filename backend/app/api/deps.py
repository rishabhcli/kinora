"""FastAPI dependencies: container access, current-user, and a Redis rate limiter.

* :func:`get_container` hands routes the wired :class:`app.composition.Container`
  off ``app.state``.
* :func:`get_current_user` validates the ``Authorization: Bearer`` JWT and loads
  the user row (401 on a missing/expired/unknown token).
* :class:`RateLimiter` is a self-contained **Redis token bucket** (one atomic Lua
  script, no extra dependency) applied to auth + write routes; it fails *open* if
  Redis is unreachable so a cache blip never takes the API down.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.errors import APIError
from app.api.security import TokenError, decode_access_token
from app.composition import Container
from app.core.logging import get_logger
from app.db.models.user import User
from app.db.repositories.user import UserRepo

logger = get_logger("app.api.deps")

# --- rate-limit policy (tokens; refill per second) --------------------------- #
AUTH_RATE_CAPACITY = 30
AUTH_RATE_REFILL_PER_S = 0.5
WRITE_RATE_CAPACITY = 60
WRITE_RATE_REFILL_PER_S = 1.0

# Atomic token bucket: refill by elapsed time, spend one token if available.
#   KEYS = [bucket]   ARGV = [capacity, refill_per_ms, now_ms, ttl_ms, cost]
#   -> {allowed(0|1), tokens(string)}
_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local cost = tonumber(ARGV[5])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity; ts = now end
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * refill)
local allowed = 0
if tokens >= cost then tokens = tokens - cost; allowed = 1 end
redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, ttl)
return {allowed, tostring(tokens)}
"""

_bearer = HTTPBearer(auto_error=False)


def get_container(request: Request) -> Container:
    """Return the wired :class:`Container` built into ``app.state`` at startup."""
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - guards a misconfigured app
        raise APIError("internal_error", "application container is not initialized", status=500)
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


async def get_current_user(
    container: ContainerDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """Validate the Bearer token and load the authenticated user (401 otherwise)."""
    if credentials is None or not credentials.credentials:
        raise APIError("unauthorized", "missing bearer token", status=401)
    try:
        token = decode_access_token(credentials.credentials, container.settings)
    except TokenError as exc:
        raise APIError("unauthorized", str(exc), status=401) from exc
    async with container.session_factory() as session:
        user = await UserRepo(session).get(token.sub)
    if user is None:
        raise APIError("unauthorized", "user no longer exists", status=401)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def _token_subject(
    credentials: HTTPAuthorizationCredentials | None, container: Container
) -> str | None:
    """Best-effort subject from a Bearer token for rate-limit identity (no 401)."""
    if credentials is None or not credentials.credentials:
        return None
    try:
        return decode_access_token(credentials.credentials, container.settings).sub
    except TokenError:
        return None


class RateLimiter:
    """A per-identity Redis token-bucket rate limiter used as a route dependency."""

    def __init__(self, *, scope: str, capacity: int, refill_per_s: float) -> None:
        self._scope = scope
        self._capacity = capacity
        self._refill_per_ms = refill_per_s / 1000.0
        # Keep the bucket alive long enough to refill fully, then let it expire.
        self._ttl_ms = max(int((capacity / max(refill_per_s, 1e-6)) * 1000), 1000)

    def _identity(self, request: Request, container: Container) -> str:
        credentials: HTTPAuthorizationCredentials | None = None
        header = request.headers.get("authorization")
        if header and header.lower().startswith("bearer "):
            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=header[7:].strip()
            )
        subject = _token_subject(credentials, container)
        if subject is not None:
            return f"user:{subject}"
        client = request.client
        return f"ip:{client.host}" if client is not None else "ip:unknown"

    async def __call__(self, request: Request, container: ContainerDep) -> None:
        key = f"kinora:rl:{self._scope}:{self._identity(request, container)}"
        now_ms = int(time.time() * 1000)
        try:
            result = await container.redis.raw.eval(
                _BUCKET_LUA,
                1,
                key,
                str(self._capacity),
                repr(self._refill_per_ms),
                str(now_ms),
                str(self._ttl_ms),
                "1",
            )
        except Exception as exc:  # noqa: BLE001 - fail open: never let Redis 500 the API
            logger.warning("ratelimit.unavailable", scope=self._scope, error=str(exc))
            return
        allowed = bool(int(result[0]))
        if not allowed:
            raise APIError(
                "rate_limited",
                "too many requests; slow down",
                status=429,
                detail={"scope": self._scope, "capacity": self._capacity},
            )


#: Stricter bucket for the auth surface (credential stuffing defence).
auth_rate_limit = RateLimiter(
    scope="auth", capacity=AUTH_RATE_CAPACITY, refill_per_s=AUTH_RATE_REFILL_PER_S
)
#: General write-route bucket.
write_rate_limit = RateLimiter(
    scope="write", capacity=WRITE_RATE_CAPACITY, refill_per_s=WRITE_RATE_REFILL_PER_S
)


__all__ = [
    "AUTH_RATE_CAPACITY",
    "WRITE_RATE_CAPACITY",
    "ContainerDep",
    "CurrentUser",
    "RateLimiter",
    "auth_rate_limit",
    "get_container",
    "get_current_user",
    "write_rate_limit",
]
