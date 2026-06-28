"""Login throttling + account lockout (kinora.md §12 — credential-stuffing defence).

Two complementary layers:

* **Per-IP throttle** (this module, Redis) — a sliding counter that caps login
  *attempts* from one IP within a window, independent of which account is
  targeted. This blunts spray attacks that rotate usernames. It **fails open**
  (a Redis blip never locks everyone out), mirroring the gateway's existing
  :class:`app.api.deps.RateLimiter`.
* **Per-account lockout** (the DB ``auth_credentials`` row, driven by
  :class:`app.auth.repositories.AuthCredentialRepo`) — durable across restarts,
  resets on a successful login, surfaced as :class:`AccountLocked`.

The service layer composes both: a login first checks the per-IP throttle, then
verifies credentials, recording success/failure into the per-account counters.
"""

from __future__ import annotations

import time

from app.core.logging import get_logger
from app.redis.client import RedisClient

logger = get_logger("app.auth.lockout")

# A sliding-window counter: INCR a per-(ip) key, set its TTL on first hit, and
# read the count. KEYS=[key] ARGV=[window_ms] -> current count.
_THROTTLE_LUA = """
local key = KEYS[1]
local window = tonumber(ARGV[1])
local count = redis.call('INCR', key)
if count == 1 then
    redis.call('PEXPIRE', key, window)
end
return count
"""


class LoginThrottle:
    """A Redis sliding-window throttle on login attempts per identity (IP/email)."""

    def __init__(self, redis: RedisClient, *, max_attempts: int, window_s: int) -> None:
        self._redis = redis
        self._max = max_attempts
        self._window_ms = max(window_s * 1000, 1000)

    @staticmethod
    def _key(scope: str, identity: str) -> str:
        return f"kinora:auth:throttle:{scope}:{identity}"

    async def hit(self, identity: str, *, scope: str = "ip") -> int:
        """Record an attempt; return the current count in the window (0 on Redis error)."""
        key = self._key(scope, identity)
        try:
            count = await self._redis.raw.eval(_THROTTLE_LUA, 1, key, str(self._window_ms))
            return int(count)
        except Exception as exc:  # noqa: BLE001 - fail open
            logger.warning("auth.throttle.unavailable", error=str(exc))
            return 0

    async def is_blocked(self, identity: str, *, scope: str = "ip") -> bool:
        """Whether ``identity`` has already exceeded the attempt cap in the window."""
        key = self._key(scope, identity)
        try:
            raw = await self._redis.raw.get(key)
        except Exception as exc:  # noqa: BLE001 - fail open
            logger.warning("auth.throttle.unavailable", error=str(exc))
            return False
        if raw is None:
            return False
        return int(raw) > self._max

    async def reset(self, identity: str, *, scope: str = "ip") -> None:
        """Clear the counter for an identity (on a successful login)."""
        try:
            await self._redis.delete(self._key(scope, identity))
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.warning("auth.throttle.reset_failed", error=str(exc))

    async def retry_after_s(self, identity: str, *, scope: str = "ip") -> int:
        """Seconds until the window resets (for a ``Retry-After`` hint)."""
        key = self._key(scope, identity)
        try:
            pttl = await self._redis.raw.pttl(key)
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.warning("auth.throttle.pttl_failed", error=str(exc))
            return 0
        return max(int(pttl) // 1000, 0) if pttl and pttl > 0 else 0


class RevocationStore:
    """A Redis-backed access-token (``jti``) denylist for the token service.

    Access tokens are stateless, so revoking *one* before its natural expiry means
    remembering its ``jti`` until then. A short-TTL key per revoked ``jti`` is the
    cheapest correct implementation; it fails open on a Redis blip (a revoked
    token still expires on its own, so the blast radius is bounded by the access
    TTL).
    """

    def __init__(self, redis: RedisClient) -> None:
        self._redis = redis

    @staticmethod
    def _key(jti: str) -> str:
        return f"kinora:auth:revoked:{jti}"

    async def is_revoked(self, jti: str) -> bool:
        """Whether the access token ``jti`` is on the denylist."""
        try:
            return bool(await self._redis.raw.exists(self._key(jti)))
        except Exception as exc:  # noqa: BLE001 - fail open
            logger.warning("auth.revocation.unavailable", error=str(exc))
            return False

    async def revoke(self, jti: str, *, ttl_s: int) -> None:
        """Denylist ``jti`` for ``ttl_s`` seconds."""
        try:
            await self._redis.raw.set(self._key(jti), str(int(time.time())), ex=max(ttl_s, 1))
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.warning("auth.revocation.set_failed", error=str(exc))


__all__ = ["LoginThrottle", "RevocationStore"]
