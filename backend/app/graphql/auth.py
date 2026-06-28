"""API-key authentication, scopes, and per-key rate limiting for the gateway.

The public GraphQL surface is authenticated by **API keys** (not the desktop
app's user JWT), so third-party integrations can be granted scoped, revocable,
rate-limited access without sharing a user's session token. This module owns:

* the API-key format ``kinora_pk_<id>.<secret>`` and its creation/parse/verify;
* the Redis-backed key store (a hash per key id; only a SHA-256 of the secret is
  stored, never the secret itself — mirrors how passwords are hashed in
  ``app/api/security.py``);
* the :class:`Scope` set and a scope-check helper;
* a per-key Redis token-bucket rate limiter reusing the same atomic-Lua approach
  as ``app/api/deps.py`` (fail-open so a Redis blip never 500s the gateway).

Keys are stored in Redis (no DB migration), keeping this package self-contained
and parallel-safe with the other worktrees.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.graphql.errors import forbidden, rate_limited, unauthenticated

if TYPE_CHECKING:
    from app.redis.client import RedisClient

_KEY_PREFIX = "kinora_pk_"
_REDIS_KEY = "kinora:gql:key:"
_REDIS_RL = "kinora:gql:rl:"
_ID_BYTES = 9  # ~14 base32-ish chars
_SECRET_BYTES = 24


class Scope:
    """The fixed set of API-key scopes (a key holds a subset)."""

    BOOKS_READ = "books:read"
    SESSIONS_READ = "sessions:read"
    SESSIONS_WRITE = "sessions:write"
    CANON_READ = "canon:read"
    CANON_WRITE = "canon:write"
    DIRECTOR_WRITE = "director:write"
    PREFS_READ = "prefs:read"

    ALL = (
        BOOKS_READ,
        SESSIONS_READ,
        SESSIONS_WRITE,
        CANON_READ,
        CANON_WRITE,
        DIRECTOR_WRITE,
        PREFS_READ,
    )
    #: A sensible read-only default for a freshly minted key.
    READ_ONLY = (BOOKS_READ, SESSIONS_READ, CANON_READ, PREFS_READ)


#: Per-key requests-per-minute default (overridable per key).
DEFAULT_RPM = 120

# Atomic token bucket (capacity, refill_per_ms, now_ms, ttl_ms, cost) -> {allowed, tokens}.
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


@dataclass(slots=True)
class ApiKeyRecord:
    """The stored metadata for an API key (never holds the plaintext secret)."""

    key_id: str
    user_id: str
    secret_hash: str
    scopes: tuple[str, ...]
    label: str
    rpm: int = DEFAULT_RPM
    created_at: float = field(default_factory=time.time)
    revoked: bool = False

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def public_view(self) -> dict[str, Any]:
        """A redacted view safe to return to the owning user (no secret hash)."""
        return {
            "keyId": self.key_id,
            "userId": self.user_id,
            "scopes": list(self.scopes),
            "label": self.label,
            "rpm": self.rpm,
            "createdAt": self.created_at,
            "revoked": self.revoked,
        }


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_key() -> tuple[str, str, str]:
    """Mint a new ``(full_key, key_id, secret)`` triple.

    ``full_key`` is what the caller shows the user once; only ``_hash_secret`` of
    the secret is persisted.
    """
    key_id = secrets.token_hex(_ID_BYTES)
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    return f"{_KEY_PREFIX}{key_id}.{secret}", key_id, secret


def parse_key(raw: str) -> tuple[str, str] | None:
    """Split a presented key into ``(key_id, secret)`` or ``None`` if malformed."""
    if not raw or not raw.startswith(_KEY_PREFIX):
        return None
    body = raw[len(_KEY_PREFIX) :]
    if "." not in body:
        return None
    key_id, secret = body.split(".", 1)
    if not key_id or not secret:
        return None
    return key_id, secret


def extract_key(headers: Any) -> str | None:
    """Pull a presented API key from ``X-API-Key`` or a ``Bearer`` Authorization."""
    api_key = headers.get("x-api-key") if hasattr(headers, "get") else None
    if api_key:
        return str(api_key).strip()
    auth = headers.get("authorization") if hasattr(headers, "get") else None
    if auth and auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token.startswith(_KEY_PREFIX):
            return token
    return None


class ApiKeyStore:
    """A Redis-backed API-key store (no DB migration; self-contained)."""

    def __init__(self, redis: RedisClient) -> None:
        self._redis = redis

    async def create(
        self, *, user_id: str, scopes: tuple[str, ...], label: str, rpm: int = DEFAULT_RPM
    ) -> tuple[str, ApiKeyRecord]:
        """Create a key, persist its record, return ``(full_key, record)``.

        The plaintext key is only returned here; it cannot be recovered later.
        """
        invalid = [s for s in scopes if s not in Scope.ALL]
        if invalid:
            raise ValueError(f"unknown scope(s): {invalid}")
        full_key, key_id, secret = generate_key()
        record = ApiKeyRecord(
            key_id=key_id,
            user_id=user_id,
            secret_hash=_hash_secret(secret),
            scopes=tuple(scopes),
            label=label[:200],
            rpm=max(1, int(rpm)),
        )
        await self._redis.set_json(_REDIS_KEY + key_id, _record_to_json(record))
        return full_key, record

    async def get(self, key_id: str) -> ApiKeyRecord | None:
        raw = await self._redis.get_json(_REDIS_KEY + key_id)
        if not isinstance(raw, dict):
            return None
        return _record_from_json(raw)

    async def verify(self, raw_key: str) -> ApiKeyRecord:
        """Authenticate a presented key, raising ``UNAUTHENTICATED`` on any failure."""
        parsed = parse_key(raw_key)
        if parsed is None:
            raise unauthenticated("Malformed API key.")
        key_id, secret = parsed
        record = await self.get(key_id)
        if record is None or record.revoked:
            raise unauthenticated("Unknown or revoked API key.")
        if not hmac.compare_digest(record.secret_hash, _hash_secret(secret)):
            raise unauthenticated("Invalid API key.")
        return record

    async def revoke(self, key_id: str) -> bool:
        record = await self.get(key_id)
        if record is None:
            return False
        record.revoked = True
        await self._redis.set_json(_REDIS_KEY + key_id, _record_to_json(record))
        return True

    async def list_for_user(self, user_id: str) -> list[ApiKeyRecord]:
        """All (non-secret) key records owned by a user."""
        out: list[ApiKeyRecord] = []
        raw = self._redis.raw
        cursor = 0
        while True:
            cursor, batch = await raw.scan(cursor=cursor, match=f"{_REDIS_KEY}*", count=200)
            for full in batch:
                key_id = full[len(_REDIS_KEY) :]
                record = await self.get(key_id)
                if record is not None and record.user_id == user_id:
                    out.append(record)
            if cursor == 0:
                break
        out.sort(key=lambda r: r.created_at, reverse=True)
        return out


def require_scope(record: ApiKeyRecord, scope: str) -> None:
    """Raise ``FORBIDDEN`` unless the key holds ``scope``."""
    if not record.has_scope(scope):
        raise forbidden(f"This API key lacks the required scope {scope!r}.")


class RateLimiter:
    """A per-key Redis token-bucket limiter (fail-open like the REST limiter)."""

    def __init__(self, redis: RedisClient) -> None:
        self._redis = redis

    async def check(self, record: ApiKeyRecord, *, cost: int = 1) -> None:
        """Spend ``cost`` tokens for this key, raising ``RATE_LIMITED`` when empty."""
        capacity = max(1, record.rpm)
        refill_per_ms = capacity / 60_000.0  # rpm → tokens/ms
        ttl_ms = max(int(capacity / max(refill_per_ms, 1e-9)), 60_000)
        now_ms = int(time.time() * 1000)
        key = f"{_REDIS_RL}{record.key_id}"
        try:
            result = await self._redis.raw.eval(
                _BUCKET_LUA,
                1,
                key,
                str(capacity),
                repr(refill_per_ms),
                str(now_ms),
                str(ttl_ms),
                str(max(1, cost)),
            )
        except Exception:  # noqa: BLE001 - fail open; a Redis blip must not 500 the gateway
            return
        if not bool(int(result[0])):
            raise rate_limited(
                "API key rate limit exceeded; slow down.",
                rpm=record.rpm,
            )


def _record_to_json(record: ApiKeyRecord) -> dict[str, Any]:
    return {
        "key_id": record.key_id,
        "user_id": record.user_id,
        "secret_hash": record.secret_hash,
        "scopes": list(record.scopes),
        "label": record.label,
        "rpm": record.rpm,
        "created_at": record.created_at,
        "revoked": record.revoked,
    }


def _record_from_json(raw: dict[str, Any]) -> ApiKeyRecord:
    return ApiKeyRecord(
        key_id=str(raw["key_id"]),
        user_id=str(raw["user_id"]),
        secret_hash=str(raw["secret_hash"]),
        scopes=tuple(raw.get("scopes", ())),
        label=str(raw.get("label", "")),
        rpm=int(raw.get("rpm", DEFAULT_RPM)),
        created_at=float(raw.get("created_at", 0.0)),
        revoked=bool(raw.get("revoked", False)),
    )


__all__ = [
    "DEFAULT_RPM",
    "ApiKeyRecord",
    "ApiKeyStore",
    "RateLimiter",
    "Scope",
    "extract_key",
    "generate_key",
    "parse_key",
    "require_scope",
]
