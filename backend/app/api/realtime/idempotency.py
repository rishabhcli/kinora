"""Idempotency keys for unsafe POSTs (kinora.md §12 robustness).

A reader on a flaky network taps "regenerate this shot" and the request times
out client-side, so the client retries — but the *first* request actually
landed, and now the same Director comment regenerates the shot twice (and, with
live video on, double-spends the budget). The standard fix is an idempotency key:
the client mints a unique ``Idempotency-Key`` per logical action; the server
remembers the first response for that key and **replays** it on any retry instead
of re-executing.

This module is the server half, modelled on Stripe's semantics:

* **First request** for a key: a lock is taken (``SET NX``), the handler runs,
  its response (status + JSON body) is stored under the key for ``ttl_s``, and
  the lock is released. The response carries ``Idempotency-Key`` + an
  ``Idempotent-Replayed: false`` header.
* **Retry after completion**: the stored response is returned verbatim with
  ``Idempotent-Replayed: true`` — the handler never runs again.
* **Retry while the first is still in flight** (lock held, no stored response
  yet): ``409 idempotency_conflict`` — the client should back off and retry,
  not race a duplicate execution.
* **Same key, *different* request body**: ``422 idempotency_key_reuse`` — reusing
  a key for a semantically different action is a client bug we refuse to mask.
  The request fingerprint (method + path + a hash of the body) is stored
  alongside the response so the mismatch is detectable.

The key is **scoped per user** so two users can't collide (or probe) on a shared
key, and the whole mechanism **fails open**: if Redis is unavailable the handler
simply runs without dedup (availability over the at-most-once guarantee), the
same trade-off the rate limiter makes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger

logger = get_logger("app.api.realtime.idempotency")

#: How long a recorded response is replayable. 24h covers any realistic retry
#: storm while bounding storage; after that a key is forgotten and re-runnable.
DEFAULT_TTL_S = 86_400
#: Max characters of a request body folded into the fingerprint (bounded work).
_FINGERPRINT_BODY_CAP = 65_536

# Atomic first-or-existing: try to claim the key (SET NX a "pending" marker with
# the request fingerprint). Returns one of:
#   {"new", fingerprint}            -- we own it; run the handler
#   {"pending", stored_fingerprint} -- someone else is mid-flight (409)
#   {"done", record_json}           -- a completed response exists (replay/mismatch)
#   KEYS = [key]
#   ARGV = [fingerprint, ttl_ms]
_CLAIM_LUA = """
local key = KEYS[1]
local fingerprint = ARGV[1]
local ttl = tonumber(ARGV[2])
local existing = redis.call('HGETALL', key)
if #existing == 0 then
    redis.call('HSET', key, 'state', 'pending', 'fingerprint', fingerprint)
    redis.call('PEXPIRE', key, ttl)
    return {'new', fingerprint}
end
local map = {}
for i = 1, #existing, 2 do map[existing[i]] = existing[i + 1] end
if map['state'] == 'done' then
    return {'done', map['record'] or '', map['fingerprint'] or ''}
end
return {'pending', map['fingerprint'] or ''}
"""


@dataclass(frozen=True, slots=True)
class StoredResponse:
    """A recorded response a retry replays verbatim."""

    status: int
    body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class IdempotencyOutcome:
    """What :meth:`IdempotencyStore.begin` decided for this request.

    Exactly one of the flags is meaningful:

    * ``proceed`` — first sight of the key; run the handler then :meth:`record`.
    * ``replay`` — a stored response is present; return it (don't run).
    * ``conflict`` — first request still in flight; answer 409.
    * ``mismatch`` — key reused with a different body; answer 422.
    """

    proceed: bool = False
    replay: StoredResponse | None = None
    conflict: bool = False
    mismatch: bool = False


def fingerprint(method: str, path: str, body: bytes | None) -> str:
    """A stable hash of (method, path, body) to detect key reuse on a different call."""
    hasher = hashlib.sha256()
    hasher.update(method.upper().encode("ascii", "ignore"))
    hasher.update(b"\x00")
    hasher.update(path.encode("utf-8", "ignore"))
    hasher.update(b"\x00")
    if body:
        hasher.update(body[:_FINGERPRINT_BODY_CAP])
    return hasher.hexdigest()


class IdempotencyStore:
    """Redis-backed at-most-once executor for keyed, unsafe requests."""

    def __init__(self, redis: Any, *, namespace: str = "kinora:idem", ttl_s: int = DEFAULT_TTL_S):
        self._redis: Any = getattr(redis, "raw", redis)
        self._ns = namespace
        self._ttl_ms = max(int(ttl_s * 1000), 1000)

    def _key(self, user_id: str, scope: str, idem_key: str) -> str:
        return f"{self._ns}:{scope}:{user_id}:{idem_key}"

    async def begin(
        self, *, user_id: str, scope: str, idem_key: str, request_fingerprint: str
    ) -> IdempotencyOutcome:
        """Claim a key (or surface the existing state). Fail-open on Redis errors."""
        key = self._key(user_id, scope, idem_key)
        try:
            raw = await self._redis.eval(
                _CLAIM_LUA, 1, key, request_fingerprint, str(self._ttl_ms)
            )
        except Exception as exc:  # noqa: BLE001 - availability over at-most-once
            logger.warning("idempotency.unavailable", scope=scope, error=str(exc))
            return IdempotencyOutcome(proceed=True)

        state = raw[0]
        if state == "new":
            return IdempotencyOutcome(proceed=True)
        if state == "pending":
            return IdempotencyOutcome(conflict=True)
        # state == "done": replay iff the fingerprint matches, else reuse error.
        record_json = raw[1] if len(raw) > 1 else ""
        stored_fp = raw[2] if len(raw) > 2 else ""
        if stored_fp and stored_fp != request_fingerprint:
            return IdempotencyOutcome(mismatch=True)
        stored = _decode_record(record_json)
        if stored is None:
            # Marker present but no body recorded yet (a crash between claim and
            # record): treat as still-pending rather than replay an empty body.
            return IdempotencyOutcome(conflict=True)
        return IdempotencyOutcome(replay=stored)

    async def record(
        self,
        *,
        user_id: str,
        scope: str,
        idem_key: str,
        request_fingerprint: str,
        status: int,
        body: dict[str, Any],
    ) -> None:
        """Persist the handler's response so later retries replay it."""
        key = self._key(user_id, scope, idem_key)
        record = json.dumps({"status": status, "body": body}, separators=(",", ":"))
        try:
            await self._redis.hset(
                key,
                mapping={"state": "done", "fingerprint": request_fingerprint, "record": record},
            )
            await self._redis.pexpire(key, self._ttl_ms)
        except Exception as exc:  # noqa: BLE001
            logger.warning("idempotency.record_failed", scope=scope, error=str(exc))

    async def release(self, *, user_id: str, scope: str, idem_key: str) -> None:
        """Drop a *pending* claim so a failed handler doesn't wedge the key.

        Called when the handler raised before :meth:`record`: the key is cleared
        only if it's still ``pending`` (a completed key is preserved for replay).
        """
        key = self._key(user_id, scope, idem_key)
        try:
            state = await self._redis.hget(key, "state")
            if state == "pending":
                await self._redis.delete(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("idempotency.release_failed", scope=scope, error=str(exc))


def _decode_record(record_json: str) -> StoredResponse | None:
    if not record_json:
        return None
    try:
        data = json.loads(record_json)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or "status" not in data:
        return None
    body = data.get("body")
    return StoredResponse(status=int(data["status"]), body=body if isinstance(body, dict) else {})


__all__ = [
    "DEFAULT_TTL_S",
    "IdempotencyOutcome",
    "IdempotencyStore",
    "StoredResponse",
    "fingerprint",
]
