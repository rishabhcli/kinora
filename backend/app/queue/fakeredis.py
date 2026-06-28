"""A dependency-free, in-process async Redis double for the render queue.

The production queue (:class:`app.queue.redis_queue.RedisRenderQueue`) and worker
talk to Redis through a small, well-defined surface — sorted sets, hashes, sets,
strings, lists, two Lua scripts (``eval``), ``scan``, TTLs, and a pub/sub bridge.
Standing up a real Redis (``KINORA_TEST_REDIS_URL``) just to unit-test queue
*logic* is heavy and made 18 of the queue's behaviours skip in CI.

:class:`FakeAsyncRedis` implements exactly that surface in plain Python so the
whole distributed job system — idempotency, lane priority, preemption,
backpressure, cancellation, leases, retries→DLQ, autoscaling — is testable with
**zero infra**. It is *not* a general Redis emulator; it covers the commands the
queue uses and raises on anything it doesn't, so a drift in the queue's Redis
usage fails loudly instead of silently no-op'ing.

Two pieces make it a drop-in for the queue:

* :class:`FakeAsyncRedis` mimics ``redis.asyncio.Redis`` (``decode_responses=True``
  semantics — every value comes back as ``str``), including a faithful enough
  ``eval`` interpreter for the queue's two Lua scripts. The queue reaches it via
  ``getattr(redis, "raw", redis)``, so the queue accepts the fake directly.
* :class:`FakeRedisClient` wraps it with the :class:`app.redis.client.RedisClient`
  JSON + pub/sub surface (``get_json`` / ``set_json`` / ``publish`` / ``subscribe``
  / ``next_message`` / ``lock``) so the *worker* — which publishes events and
  persists conflict objects — can run against it too.

The Lua interpreter is intentionally tiny: rather than embed a Lua VM, it pattern-
matches the two known scripts by content and runs an equivalent Python routine.
:data:`KNOWN_SCRIPTS` records the fingerprints, so if a script changes the fake
raises a clear error pointing here instead of returning wrong results.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from app.queue.redis_queue import _CLAIM_LUA, _ENQUEUE_LUA

__all__ = [
    "FakeAsyncRedis",
    "FakePubSub",
    "FakeRedisClient",
    "UnsupportedCommandError",
    "UnknownScriptError",
    "build_fake_queue",
]


class UnsupportedCommandError(NotImplementedError):
    """Raised when the queue calls a Redis command the fake does not model."""


class UnknownScriptError(RuntimeError):
    """Raised when ``eval`` is handed a Lua script the fake cannot interpret.

    The fake recognises the queue's scripts by a content fingerprint; an
    unrecognised script means the queue's Lua changed and this interpreter must be
    updated to match (the alternative — guessing — would silently corrupt state).
    """


def _fingerprint(script: str) -> str:
    """Stable hash of a Lua script's normalised text (whitespace-insensitive)."""
    normalised = " ".join(script.split())
    return hashlib.sha256(normalised.encode()).hexdigest()[:16]


#: Fingerprints of the Lua scripts this interpreter knows how to run, mapped to
#: the handler name. Recomputed at import so a script edit flips the fingerprint
#: and :meth:`FakeAsyncRedis.eval` raises :class:`UnknownScriptError`.
KNOWN_SCRIPTS: dict[str, str] = {
    _fingerprint(_ENQUEUE_LUA): "enqueue",
    _fingerprint(_CLAIM_LUA): "claim",
}


@dataclass
class _ZSet:
    """A sorted set: member -> score, ordered by (score, member) on read."""

    scores: dict[str, float] = field(default_factory=dict)

    def add(self, member: str, score: float) -> int:
        new = member not in self.scores
        self.scores[member] = score
        return 1 if new else 0

    def rem(self, member: str) -> int:
        return 1 if self.scores.pop(member, None) is not None else 0

    def card(self) -> int:
        return len(self.scores)

    def score(self, member: str) -> float | None:
        return self.scores.get(member)

    def range_by_score(
        self, lo: float, hi: float, *, limit: tuple[int, int] | None = None
    ) -> list[str]:
        items = sorted(
            ((s, m) for m, s in self.scores.items() if lo <= s <= hi),
            key=lambda pair: (pair[0], pair[1]),
        )
        members = [m for _s, m in items]
        if limit is not None:
            offset, count = limit
            members = members[offset : offset + count] if count >= 0 else members[offset:]
        return members


#: Module-level alias so annotations inside :class:`FakeAsyncRedis` can name the
#: builtin ``set`` even though the class defines a ``set`` *method* (Redis SET),
#: which would otherwise shadow the builtin in the class namespace for mypy.
StrSet = set[str]


def _coerce_bound(value: Any) -> float:
    """Map a Redis score-range bound (``-inf`` / ``+inf`` / number) to a float."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    if text in ("-inf", "-Inf", "-INF"):
        return float("-inf")
    if text in ("+inf", "inf", "+Inf", "INF"):
        return float("inf")
    return float(text)


class FakeAsyncRedis:
    """An in-memory async double for ``redis.asyncio.Redis`` (decode_responses).

    Every stored value is a ``str`` and every read returns ``str`` (or ``None``),
    matching the queue's ``decode_responses=True`` client. Commands operate on a
    single shared keyspace; TTLs are honoured lazily (a key past its expiry is
    treated as absent on the next access).
    """

    def __init__(self) -> None:
        self._strings: dict[str, str] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._sets: dict[str, set[str]] = {}
        self._zsets: dict[str, _ZSet] = {}
        self._lists: dict[str, list[str]] = {}
        self._expiry: dict[str, float] = {}
        self._channels: dict[str, list[FakePubSub]] = {}
        self._clock = time.monotonic
        self.closed = False

    # -- expiry bookkeeping -------------------------------------------------- #

    def _expired(self, key: str) -> bool:
        deadline = self._expiry.get(key)
        if deadline is not None and self._clock() >= deadline:
            self._drop(key)
            return True
        return False

    def _drop(self, key: str) -> None:
        self._strings.pop(key, None)
        self._hashes.pop(key, None)
        self._sets.pop(key, None)
        self._zsets.pop(key, None)
        self._lists.pop(key, None)
        self._expiry.pop(key, None)

    def _live(self, key: str) -> bool:
        return not self._expired(key)

    # -- strings ------------------------------------------------------------- #

    async def get(self, key: str) -> str | None:
        if not self._live(key):
            return None
        return self._strings.get(key)

    async def set(
        self,
        key: str,
        value: Any,
        *,
        nx: bool = False,
        px: int | None = None,
        ex: int | None = None,
    ) -> bool | None:
        self._expired(key)
        if nx and key in self._strings:
            return None
        self._strings[key] = str(value)
        if px is not None:
            self._expiry[key] = self._clock() + px / 1000.0
        elif ex is not None:
            self._expiry[key] = self._clock() + ex
        else:
            self._expiry.pop(key, None)
        return True

    async def incr(self, key: str, amount: int = 1) -> int:
        self._expired(key)
        current = int(self._strings.get(key, "0") or 0)
        current += amount
        self._strings[key] = str(current)
        return current

    async def expire(self, key: str, seconds: int) -> bool:
        if not self._live(key) or not self._key_exists(key):
            return False
        self._expiry[key] = self._clock() + seconds
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self._key_exists(key):
                self._drop(key)
                removed += 1
        return removed

    def _key_exists(self, key: str) -> bool:
        if self._expired(key):
            return False
        return (
            key in self._strings
            or key in self._hashes
            or key in self._sets
            or key in self._zsets
            or key in self._lists
        )

    # -- hashes -------------------------------------------------------------- #

    async def hset(
        self,
        key: str,
        field_name: str | None = None,
        value: Any = None,
        *,
        mapping: dict[str, Any] | None = None,
    ) -> int:
        self._expired(key)
        h = self._hashes.setdefault(key, {})
        added = 0
        if mapping is not None:
            for k, v in mapping.items():
                if k not in h:
                    added += 1
                h[k] = str(v)
        if field_name is not None:
            if field_name not in h:
                added += 1
            h[field_name] = str(value)
        return added

    async def hget(self, key: str, field_name: str) -> str | None:
        if not self._live(key):
            return None
        return self._hashes.get(key, {}).get(field_name)

    async def hgetall(self, key: str) -> dict[str, str]:
        if not self._live(key):
            return {}
        return dict(self._hashes.get(key, {}))

    # -- sets ---------------------------------------------------------------- #

    async def sadd(self, key: str, *members: str) -> int:
        self._expired(key)
        s = self._sets.setdefault(key, set())
        added = 0
        for m in members:
            if m not in s:
                s.add(m)
                added += 1
        return added

    async def srem(self, key: str, *members: str) -> int:
        if not self._live(key):
            return 0
        s = self._sets.get(key)
        if s is None:
            return 0
        removed = 0
        for m in members:
            if m in s:
                s.discard(m)
                removed += 1
        if not s:
            self._sets.pop(key, None)
        return removed

    async def smembers(self, key: str) -> StrSet:
        if not self._live(key):
            return set()
        return set(self._sets.get(key, set()))

    async def scard(self, key: str) -> int:
        if not self._live(key):
            return 0
        return len(self._sets.get(key, set()))

    # -- sorted sets --------------------------------------------------------- #

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self._expired(key)
        z = self._zsets.setdefault(key, _ZSet())
        return sum(z.add(m, float(s)) for m, s in mapping.items())

    async def zrem(self, key: str, *members: str) -> int:
        if not self._live(key):
            return 0
        z = self._zsets.get(key)
        if z is None:
            return 0
        removed = sum(z.rem(m) for m in members)
        if not z.scores:
            self._zsets.pop(key, None)
        return removed

    async def zcard(self, key: str) -> int:
        if not self._live(key):
            return 0
        z = self._zsets.get(key)
        return z.card() if z else 0

    async def zscore(self, key: str, member: str) -> float | None:
        if not self._live(key):
            return None
        z = self._zsets.get(key)
        return z.score(member) if z else None

    async def zrangebyscore(
        self,
        key: str,
        minimum: Any,
        maximum: Any,
        *,
        start: int | None = None,
        num: int | None = None,
    ) -> list[str]:
        if not self._live(key):
            return []
        z = self._zsets.get(key)
        if z is None:
            return []
        limit = (start, num) if start is not None and num is not None else None
        return z.range_by_score(_coerce_bound(minimum), _coerce_bound(maximum), limit=limit)

    # -- lists --------------------------------------------------------------- #

    async def lpush(self, key: str, *values: str) -> int:
        self._expired(key)
        lst = self._lists.setdefault(key, [])
        for v in values:
            lst.insert(0, str(v))
        return len(lst)

    async def rpush(self, key: str, *values: str) -> int:
        self._expired(key)
        lst = self._lists.setdefault(key, [])
        lst.extend(str(v) for v in values)
        return len(lst)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        if not self._live(key):
            return []
        lst = self._lists.get(key, [])
        if end == -1:
            return list(lst[start:])
        return list(lst[start : end + 1])

    async def llen(self, key: str) -> int:
        if not self._live(key):
            return 0
        return len(self._lists.get(key, []))

    async def lrem(self, key: str, count: int, value: str) -> int:
        if not self._live(key):
            return 0
        lst = self._lists.get(key)
        if lst is None:
            return 0
        before = len(lst)
        target = str(value)
        if count == 0:
            lst[:] = [v for v in lst if v != target]
        elif count > 0:
            removed = 0
            out: list[str] = []
            for v in lst:
                if v == target and removed < count:
                    removed += 1
                    continue
                out.append(v)
            lst[:] = out
        else:  # count < 0: from tail
            removed = 0
            out = []
            for v in reversed(lst):
                if v == target and removed < -count:
                    removed += 1
                    continue
                out.append(v)
            lst[:] = list(reversed(out))
        if not lst:
            self._lists.pop(key, None)
        return before - len(lst)

    # -- scan ---------------------------------------------------------------- #

    async def scan(
        self, cursor: int = 0, *, match: str | None = None, count: int = 10
    ) -> tuple[int, list[str]]:
        # Single-shot scan: gather every live key matching ``match`` and return
        # cursor 0. The queue only uses scan for namespace purge, so a one-pass
        # implementation is faithful to its needs.
        keys: StrSet = set()
        for bucket in (self._strings, self._hashes, self._sets, self._zsets, self._lists):
            keys.update(bucket.keys())
        live = [k for k in keys if self._live(k)]
        if match is not None:
            live = [k for k in live if fnmatch.fnmatch(k, match)]
        return 0, sorted(live)

    # -- eval (Lua) ---------------------------------------------------------- #

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        handler = KNOWN_SCRIPTS.get(_fingerprint(script))
        keys = [str(a) for a in args[:numkeys]]
        argv = [str(a) for a in args[numkeys:]]
        if handler == "enqueue":
            return self._eval_enqueue(keys, argv)
        if handler == "claim":
            return self._eval_claim(keys, argv)
        raise UnknownScriptError(
            "FakeAsyncRedis.eval got an unknown Lua script; if the queue's Lua "
            "changed, update KNOWN_SCRIPTS + the interpreter in app/queue/fakeredis.py"
        )

    def _eval_enqueue(self, keys: list[str], argv: list[str]) -> list[str]:
        shot_key, job_key, lane_key, lane_c, lane_s, lane_k, token_key = keys
        job_id, priority, fields_json, threshold, has_token, ready_at, token_ttl = argv
        # Idempotency: a known shot returns its existing job_id, no new job.
        existing = self._sync_get(shot_key)
        if existing is not None:
            return ["existing", existing]
        # Backpressure: drop a new *speculative* job once total depth crosses the bar.
        if priority == "speculative":
            depth = self._sync_zcard(lane_c) + self._sync_zcard(lane_s) + self._sync_zcard(lane_k)
            if depth >= int(threshold):
                return ["dropped", ""]
        fields = json.loads(fields_json)
        h = self._hashes.setdefault(job_key, {})
        for k, v in fields.items():
            h[k] = str(v)
        self._strings[shot_key] = job_id
        self._expiry.pop(shot_key, None)
        self._zsets.setdefault(lane_key, _ZSet()).add(job_id, float(ready_at))
        if has_token == "1":
            self._sets.setdefault(token_key, set()).add(job_id)
            self._expiry[token_key] = self._clock() + int(token_ttl)
        return ["enqueued", job_id]

    def _eval_claim(self, keys: list[str], argv: list[str]) -> str | bool:
        *lanes, processing = keys
        now = float(argv[0])
        lease = float(argv[1])
        for lane in lanes:
            z = self._zsets.get(lane)
            if z is None:
                continue
            ready = z.range_by_score(float("-inf"), now, limit=(0, 1))
            if ready:
                job_id = ready[0]
                z.rem(job_id)
                if not z.scores:
                    self._zsets.pop(lane, None)
                self._zsets.setdefault(processing, _ZSet()).add(job_id, now + lease)
                return job_id
        return False

    # -- sync helpers for the Lua interpreter -------------------------------- #

    def _sync_get(self, key: str) -> str | None:
        if self._expired(key):
            return None
        return self._strings.get(key)

    def _sync_zcard(self, key: str) -> int:
        if self._expired(key):
            return 0
        z = self._zsets.get(key)
        return z.card() if z else 0

    # -- pub/sub ------------------------------------------------------------- #

    async def publish(self, channel: str, message: str) -> int:
        subs = self._channels.get(channel, [])
        for sub in list(subs):
            sub._deliver(channel, message)
        return len(subs)

    def _subscribe(self, channel: str) -> FakePubSub:
        ps = FakePubSub(self, channel)
        self._channels.setdefault(channel, []).append(ps)
        return ps

    def _unsubscribe(self, ps: FakePubSub) -> None:
        subs = self._channels.get(ps.channel)
        if subs and ps in subs:
            subs.remove(ps)
        if subs is not None and not subs:
            self._channels.pop(ps.channel, None)

    def pubsub(self) -> FakePubSub:
        # The raw redis pubsub() factory is unused by the queue; the JSON client
        # subscribes through FakeRedisClient.subscribe instead.
        raise UnsupportedCommandError("use FakeRedisClient.subscribe for pub/sub")

    async def aclose(self) -> None:
        self.closed = True

    async def close(self) -> None:
        self.closed = True

    # Anything unmodelled fails loudly rather than silently no-op'ing.
    def __getattr__(self, name: str) -> Any:
        raise UnsupportedCommandError(
            f"FakeAsyncRedis does not model the {name!r} command; "
            "add it if the queue starts using it"
        )


class FakePubSub:
    """A single-channel pub/sub subscription delivering JSON-decoded messages."""

    def __init__(self, redis: FakeAsyncRedis, channel: str) -> None:
        self._redis = redis
        self.channel = channel
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    def _deliver(self, channel: str, message: str) -> None:
        self._queue.put_nowait(message)

    async def get(self, *, timeout: float = 5.0) -> dict[str, Any] | None:
        try:
            raw = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return None
        return json.loads(raw)

    def close(self) -> None:
        self._redis._unsubscribe(self)


class _FakeLock:
    """A best-effort async lock matching ``RedisClient.lock`` semantics."""

    def __init__(self, redis: FakeAsyncRedis, name: str, ttl_ms: int) -> None:
        self._redis = redis
        self._name = f"kinora:lock:{name}"
        self._ttl_ms = ttl_ms
        self._token = f"tok-{id(self)}"
        self.acquired = False

    @property
    def token(self) -> str:
        return self._token

    async def acquire(self) -> bool:
        ok = await self._redis.set(self._name, self._token, nx=True, px=self._ttl_ms)
        self.acquired = bool(ok)
        return self.acquired

    async def release(self) -> bool:
        if await self._redis.get(self._name) == self._token:
            await self._redis.delete(self._name)
            self.acquired = False
            return True
        return False

    async def __aenter__(self) -> _FakeLock:
        await self.acquire()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.release()


class FakeRedisClient:
    """A double for :class:`app.redis.client.RedisClient` over a shared fake.

    Exposes ``.raw`` (the :class:`FakeAsyncRedis` the queue binds to) plus the
    JSON + pub/sub surface the worker uses, so a single instance backs both the
    queue and the worker in a test.
    """

    def __init__(self, raw: FakeAsyncRedis | None = None) -> None:
        self.raw: FakeAsyncRedis = raw or FakeAsyncRedis()
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def get_json(self, key: str) -> Any | None:
        raw = await self.raw.get(key)
        return json.loads(raw) if raw is not None else None

    async def set_json(self, key: str, value: Any, *, ttl_s: int | None = None) -> None:
        await self.raw.set(key, json.dumps(value), ex=ttl_s)

    async def delete(self, *keys: str) -> int:
        return await self.raw.delete(*keys)

    async def publish(self, channel: str, message: dict[str, Any]) -> int:
        # Record for assertion convenience *and* deliver to live subscribers so
        # iter_events() works against the fake exactly as against real Redis.
        self.published.append((channel, message))
        return await self.raw.publish(channel, json.dumps(message))

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[FakePubSub]:
        ps = self.raw._subscribe(channel)
        try:
            yield ps
        finally:
            ps.close()

    async def next_message(
        self, pubsub: FakePubSub, *, timeout: float = 5.0
    ) -> dict[str, Any] | None:
        return await pubsub.get(timeout=timeout)

    def lock(self, name: str, *, ttl_ms: int = 10_000, **_kw: Any) -> _FakeLock:
        return _FakeLock(self.raw, name, ttl_ms)

    async def close(self) -> None:
        await self.raw.aclose()

    def events_on(self, channel: str) -> list[dict[str, Any]]:
        """Every event published to ``channel`` so far (test convenience)."""
        return [msg for ch, msg in self.published if ch == channel]


def build_fake_queue(**kwargs: Any) -> tuple[Any, FakeRedisClient]:
    """Build a :class:`RedisRenderQueue` backed by a fresh fake + its client.

    Returns ``(queue, client)``. The queue binds to ``client.raw``; pass the same
    ``client`` to a worker so both share one keyspace + pub/sub bus. Any keyword
    is forwarded to the queue constructor (``backpressure_depth``, ``retry_cap``,
    ``clock_ms`` …), so tests can drive deterministic time.
    """
    from app.queue.redis_queue import RedisRenderQueue

    client = FakeRedisClient()
    queue = RedisRenderQueue(client, **kwargs)
    return queue, client
