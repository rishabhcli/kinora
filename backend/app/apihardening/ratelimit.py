"""A configurable token-bucket rate-limiter middleware (per principal / IP).

This is a *middleware* — it limits before a route even resolves — complementing
the per-route limiter dependency in :mod:`app.api.realtime.ratelimit` (which this
module deliberately does not touch). Features:

* **Token bucket** per identity: ``capacity`` burst, ``refill_per_s`` steady rate.
* **Per-route rules**: declare :class:`RateLimitRule` matching a method + path
  prefix with its own capacity/rate; the first matching rule wins, else the
  default bucket applies.
* **Identity** = the Bearer-token subject when present, else the client IP
  (anonymous), so one logged-in user can't be throttled by a noisy neighbour
  behind a shared NAT and vice-versa.
* **Standard headers**: IETF draft ``RateLimit-Limit`` / ``-Remaining`` / ``-Reset``
  on every response, plus ``Retry-After`` on the ``429``.
* **Fail-open**: a store outage degrades to "no limiting", never a ``5xx``.
* Two interchangeable stores behind one :class:`TokenBucketStore` protocol — an
  in-process :class:`InMemoryTokenBucketStore` (default, great for tests + a
  single instance) and a :class:`RedisTokenBucketStore` (shared across replicas,
  the same atomic Lua the rest of the gateway uses).

The 429 body matches whichever error surface the app uses (legacy
``{"error": {...}}`` or problem+json), selected by :class:`HardeningConfig`.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Protocol

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.apihardening.config import HardeningConfig
from app.core.logging import get_logger

logger = get_logger("app.apihardening.ratelimit")


@dataclass(frozen=True, slots=True)
class BucketResult:
    """The outcome of one token-bucket spend."""

    allowed: bool
    remaining: float
    capacity: int
    refill_per_s: float

    @property
    def reset_seconds(self) -> int:
        """Seconds until the bucket is full again (the ``RateLimit-Reset`` hint)."""
        if self.remaining >= self.capacity or self.refill_per_s <= 0:
            return 0
        return int(math.ceil((self.capacity - self.remaining) / self.refill_per_s))

    def retry_after_seconds(self, cost: int = 1) -> int:
        """Seconds until ``cost`` tokens exist again (the ``Retry-After`` value)."""
        if self.refill_per_s <= 0:
            return 1
        deficit = max(cost - self.remaining, 0.0)
        return max(int(math.ceil(deficit / self.refill_per_s)), 1)


class TokenBucketStore(Protocol):
    """A backend that atomically spends a token from a named bucket."""

    async def consume(
        self, key: str, *, capacity: int, refill_per_s: float, cost: int = 1
    ) -> BucketResult:
        """Refill ``key`` by elapsed time then spend ``cost`` tokens if available."""
        ...


class InMemoryTokenBucketStore:
    """A process-local token-bucket store (single instance / tests).

    Lazy continuous refill keyed on a monotonic clock; bounded by an LRU-ish cap
    so a flood of distinct identities can't grow it without limit.
    """

    def __init__(self, *, max_keys: int = 100_000, clock: Any = time.monotonic) -> None:
        self._buckets: dict[str, tuple[float, float]] = {}
        self._max_keys = max_keys
        self._clock = clock

    async def consume(
        self, key: str, *, capacity: int, refill_per_s: float, cost: int = 1
    ) -> BucketResult:
        now = self._clock()
        tokens, ts = self._buckets.get(key, (float(capacity), now))
        elapsed = max(now - ts, 0.0)
        tokens = min(float(capacity), tokens + elapsed * refill_per_s)
        allowed = tokens >= cost
        if allowed:
            tokens -= cost
        # Evict the oldest entry when over the cap (cheap, deterministic).
        if key not in self._buckets and len(self._buckets) >= self._max_keys:
            oldest = min(self._buckets, key=lambda k: self._buckets[k][1])
            self._buckets.pop(oldest, None)
        self._buckets[key] = (tokens, now)
        return BucketResult(allowed, tokens, capacity, refill_per_s)

    def clear(self) -> None:
        """Drop all buckets (test helper)."""
        self._buckets.clear()


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


def resolve_redis(redis_or_resolver: Any) -> Any:
    """Resolve a redis client from a client object or a zero-arg resolver.

    A resolver (callable) lets a store be constructed at ``create_app`` time — when
    the wired container/Redis does not exist yet — and bound to the live client
    lazily on the first request. Returns ``None`` when nothing is available yet,
    which the stores treat as fail-open.
    """
    if redis_or_resolver is None:
        return None
    if callable(redis_or_resolver):
        try:
            return redis_or_resolver()
        except Exception:  # noqa: BLE001 - a missing container resolves to None
            return None
    return redis_or_resolver


class RedisTokenBucketStore:
    """A Redis-backed token bucket (shared across replicas), fail-open.

    ``redis`` is any object exposing ``raw.eval`` like
    :class:`app.redis.client.RedisClient`, **or** a zero-arg callable returning
    one (resolved lazily on each request — see :func:`resolve_redis`). On any
    Redis error (or no client yet) the store reports "allowed" so a cache blip can
    never 5xx the gateway.
    """

    def __init__(self, redis: Any, *, key_prefix: str = "kinora:harden:rl") -> None:
        self._redis = redis
        self._prefix = key_prefix

    async def consume(
        self, key: str, *, capacity: int, refill_per_s: float, cost: int = 1
    ) -> BucketResult:
        client = resolve_redis(self._redis)
        if client is None:
            return BucketResult(True, float(capacity), capacity, refill_per_s)
        refill_per_ms = refill_per_s / 1000.0
        ttl_ms = max(int((capacity / max(refill_per_s, 1e-6)) * 1000), 1000)
        now_ms = int(time.time() * 1000)
        full_key = f"{self._prefix}:{key}"
        try:
            result = await client.raw.eval(
                _BUCKET_LUA,
                1,
                full_key,
                str(capacity),
                repr(refill_per_ms),
                str(now_ms),
                str(ttl_ms),
                str(cost),
            )
        except Exception as exc:  # noqa: BLE001 - fail open
            logger.warning("hardening.ratelimit.unavailable", error=str(exc))
            return BucketResult(True, float(capacity), capacity, refill_per_s)
        allowed = bool(int(result[0]))
        tokens = float(result[1])
        return BucketResult(allowed, tokens, capacity, refill_per_s)


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    """A per-route override matching a method set + path prefix.

    ``methods`` empty == all methods. Rules are evaluated in order; the first
    match wins. The ``scope`` is folded into the bucket key so different rules
    don't share a bucket.
    """

    scope: str
    path_prefix: str
    capacity: int
    refill_per_s: float
    methods: frozenset[str] = frozenset()
    cost: int = 1

    def matches(self, method: str, path: str) -> bool:
        if self.methods and method.upper() not in self.methods:
            return False
        return path.startswith(self.path_prefix)


def _decode_jwt_subject(token: str) -> str | None:
    """Best-effort, *unverified* subject from a JWT for bucket identity only.

    Identity here only buckets requests; it is not an auth decision (the route's
    real auth dependency still verifies the token), so an unverified ``sub`` read
    is safe and avoids importing the auth stack into the middleware. Returns
    ``None`` on any malformed token (the caller falls back to the client IP).
    """
    import base64
    import json

    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    sub = claims.get("sub")
    return str(sub) if sub is not None else None


def _identity(scope: Scope) -> str:
    """Bucket identity: the Bearer subject if present, else the client IP."""
    headers = {name.lower(): value for name, value in scope.get("headers", [])}
    auth = headers.get(b"authorization")
    if auth is not None:
        raw = auth.decode("latin-1")
        if raw.lower().startswith("bearer "):
            subject = _decode_jwt_subject(raw[7:].strip())
            if subject:
                return f"user:{subject}"
    client = scope.get("client")
    host = client[0] if client else "unknown"
    return f"ip:{host}"


class RateLimitMiddleware:
    """Token-bucket rate limiting before route resolution (per principal/IP)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        store: TokenBucketStore | None = None,
        config: HardeningConfig | None = None,
        rules: tuple[RateLimitRule, ...] = (),
    ) -> None:
        self.app = app
        self._config = config or HardeningConfig()
        self._store = store or InMemoryTokenBucketStore()
        self._rules = rules

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._config.rate_limit_enabled:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if any(path.startswith(p) for p in self._config.rate_limit_exempt_prefixes):
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        rule = next((r for r in self._rules if r.matches(method, path)), None)
        scope_name = rule.scope if rule is not None else "default"
        capacity = rule.capacity if rule is not None else self._config.rate_limit_capacity
        refill = rule.refill_per_s if rule is not None else self._config.rate_limit_refill_per_s
        cost = rule.cost if rule is not None else 1

        key = f"{scope_name}:{_identity(scope)}"
        result = await self._store.consume(
            key, capacity=capacity, refill_per_s=refill, cost=cost
        )

        rl_headers: list[tuple[bytes, bytes]] = []
        if self._config.rate_limit_emit_headers:
            rl_headers = [
                (b"ratelimit-limit", str(capacity).encode("ascii")),
                (b"ratelimit-remaining", str(max(int(result.remaining), 0)).encode("ascii")),
                (b"ratelimit-reset", str(result.reset_seconds).encode("ascii")),
            ]

        if not result.allowed:
            retry_after = result.retry_after_seconds(cost)
            await self._send_429(send, scope_name, capacity, retry_after, rl_headers)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start" and rl_headers:
                raw = list(message.get("headers", []))
                present = {name.lower() for name, _ in raw}
                for name, value in rl_headers:
                    if name not in present:
                        raw.append((name, value))
                message = {**message, "headers": raw}
            await send(message)

        await self.app(scope, receive, send_with_headers)

    async def _send_429(
        self,
        send: Send,
        scope_name: str,
        capacity: int,
        retry_after: int,
        rl_headers: list[tuple[bytes, bytes]],
    ) -> None:
        from app.apihardening.render import render_error_bytes

        body, media_type = render_error_bytes(
            code="rate_limited",
            title="Too Many Requests",
            status=429,
            detail="too many requests; slow down",
            config=self._config,
            extensions={
                "scope": scope_name,
                "capacity": capacity,
                "retry_after_s": retry_after,
            },
        )
        headers = [
            (b"content-type", media_type.encode("ascii")),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"retry-after", str(retry_after).encode("ascii")),
            *rl_headers,
        ]
        await send({"type": "http.response.start", "status": 429, "headers": headers})
        await send({"type": "http.response.body", "body": body})


__all__ = [
    "BucketResult",
    "InMemoryTokenBucketStore",
    "RateLimitMiddleware",
    "RateLimitRule",
    "RedisTokenBucketStore",
    "TokenBucketStore",
    "resolve_redis",
]
