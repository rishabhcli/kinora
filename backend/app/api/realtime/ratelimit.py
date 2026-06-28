"""Composable per-route / per-user rate limiting with standard headers (§12).

Round-1's :class:`app.api.deps.RateLimiter` is a solid Redis token bucket, but it
(a) bins everything into two coarse scopes (``auth`` / ``write``), (b) raises a
bare 429 with no ``Retry-After`` or ``RateLimit-*`` headers, and (c) can't vary
the limit per *route*. This module extends it **additively** — it does not touch
or replace the existing limiter or its `deps` instances — to add:

* **Per-route policies**: declare ``RateLimitPolicy(scope="regen", capacity=10,
  refill_per_s=0.2)`` and attach it to exactly the route that needs it.
* **Per-user identity by default**, falling back to client IP for anonymous
  calls (same identity logic as the round-1 limiter).
* **The IETF ``RateLimit`` headers** (draft ``RateLimit-Limit`` /
  ``RateLimit-Remaining`` / ``RateLimit-Reset``) on success *and* a
  ``Retry-After`` on the 429, so a well-behaved client can self-throttle instead
  of hammering.

It reuses the exact same atomic token-bucket Lua as `deps` (returning the live
token count so we can compute ``Remaining``/``Reset``) and keeps the **fail-open**
contract: a Redis outage degrades to "no limiting", never to a 5xx.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from fastapi import Request, Response

from app.api.errors import APIError
from app.api.security import TokenError, decode_access_token
from app.composition import Container
from app.core.logging import get_logger

logger = get_logger("app.api.realtime.ratelimit")

# Atomic token bucket returning {allowed, tokens_remaining}. Mirrors deps._BUCKET_LUA
# but lives here so this module is self-contained and testable in isolation.
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


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """A named bucket: ``capacity`` tokens, refilled at ``refill_per_s`` tokens/s."""

    scope: str
    capacity: int
    refill_per_s: float
    cost: int = 1


def _identity(request: Request, container: Container) -> str:
    """Per-user identity from a Bearer token, else the client IP (anonymous)."""
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        token = header[7:].strip()
        try:
            return f"user:{decode_access_token(token, container.settings).sub}"
        except TokenError:
            pass
    client = request.client
    return f"ip:{client.host}" if client is not None else "ip:unknown"


def _reset_seconds(tokens: float, capacity: int, refill_per_s: float) -> int:
    """Seconds until the bucket is full again (the ``RateLimit-Reset`` hint)."""
    if tokens >= capacity or refill_per_s <= 0:
        return 0
    return int(math.ceil((capacity - tokens) / refill_per_s))


def _retry_after_seconds(tokens: float, cost: int, refill_per_s: float) -> int:
    """Seconds until enough tokens exist to admit one more request of ``cost``."""
    if refill_per_s <= 0:
        return 1
    deficit = max(cost - tokens, 0.0)
    return max(int(math.ceil(deficit / refill_per_s)), 1)


class RouteRateLimiter:
    """A per-route limiter dependency that also writes the standard headers.

    Usage in a route::

        limiter = RouteRateLimiter(RateLimitPolicy("regen", 10, 0.2))
        @router.post(...)
        async def regen(..., _rl: Annotated[None, Depends(limiter)]): ...

    The limiter writes ``RateLimit-*`` headers onto the *response* via a injected
    :class:`fastapi.Response`, and raises a 429 (carrying ``Retry-After`` in the
    error detail and on the response) when the bucket is empty.
    """

    def __init__(self, policy: RateLimitPolicy) -> None:
        self._policy = policy
        self._refill_per_ms = policy.refill_per_s / 1000.0
        self._ttl_ms = max(int((policy.capacity / max(policy.refill_per_s, 1e-6)) * 1000), 1000)

    async def __call__(self, request: Request, response: Response) -> None:
        container = getattr(request.app.state, "container", None)
        if container is None:  # pragma: no cover - misconfigured app
            return
        key = f"kinora:rrl:{self._policy.scope}:{_identity(request, container)}"
        now_ms = int(time.time() * 1000)
        try:
            result = await container.redis.raw.eval(
                _BUCKET_LUA,
                1,
                key,
                str(self._policy.capacity),
                repr(self._refill_per_ms),
                str(now_ms),
                str(self._ttl_ms),
                str(self._policy.cost),
            )
        except Exception as exc:  # noqa: BLE001 - fail open
            logger.warning("route_ratelimit.unavailable", scope=self._policy.scope, error=str(exc))
            return

        allowed = bool(int(result[0]))
        tokens = float(result[1])
        reset = _reset_seconds(tokens, self._policy.capacity, self._policy.refill_per_s)
        response.headers["RateLimit-Limit"] = str(self._policy.capacity)
        response.headers["RateLimit-Remaining"] = str(max(int(tokens), 0))
        response.headers["RateLimit-Reset"] = str(reset)
        if not allowed:
            retry_after = _retry_after_seconds(
                tokens, self._policy.cost, self._policy.refill_per_s
            )
            response.headers["Retry-After"] = str(retry_after)
            raise APIError(
                "rate_limited",
                "too many requests; slow down",
                status=429,
                detail={
                    "scope": self._policy.scope,
                    "capacity": self._policy.capacity,
                    "retry_after_s": retry_after,
                },
            )


__all__ = [
    "RateLimitPolicy",
    "RouteRateLimiter",
]
