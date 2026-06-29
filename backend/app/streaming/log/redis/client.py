"""The Redis surface the streams broker uses + an in-process fake of it.

:class:`StreamRedis` is the *minimal* async Redis surface
:class:`~app.streaming.log.redis.broker.RedisStreamsBroker` needs: a couple of
stream commands (``xadd``/``xrange``/``xlen``/``xtrim``), hash + string commands
for metadata/offsets/sequences, an atomic counter (``incr``), set membership for
topic registration, and key delete. Keeping the surface tiny means the broker
can run over either the real ``redis.asyncio`` client (:class:`RedisStreamAdapter`)
or :class:`FakeStreamRedis`, an in-process double, with no behavioural drift.

The fake is intentionally small and faithful: every command the broker calls is
modelled; anything else raises ``NotImplementedError`` so a drift in the broker's
Redis usage fails loudly rather than silently no-op'ing (the same discipline the
queue's ``app.queue.fakeredis`` applies). All values are ``str`` in/out, matching
``decode_responses=True``.
"""

from __future__ import annotations

import fnmatch
from typing import Any, Protocol, runtime_checkable

__all__ = ["FakeStreamRedis", "RedisStreamAdapter", "StreamRedis", "StreamEntry"]

#: A stream entry as returned by ``xrange``: ``(entry_id, {field: value})``.
StreamEntry = tuple[str, dict[str, str]]

#: Alias for ``builtins.set[str]`` — the ``StreamRedis.set`` *method* shadows the
#: ``set`` builtin inside class bodies, so ``smembers`` annotations use this.
StrSet = set[str]


@runtime_checkable
class StreamRedis(Protocol):
    """The slice of Redis the streams broker depends on (all values are ``str``)."""

    async def xadd(self, key: str, fields: dict[str, str], *, entry_id: str = "*") -> str: ...

    async def xrange(
        self, key: str, start: str = "-", end: str = "+", *, count: int | None = None
    ) -> list[StreamEntry]: ...

    async def xlen(self, key: str) -> int: ...

    async def xtrim_minid(self, key: str, min_id: str) -> int: ...

    async def incr(self, key: str, amount: int = 1) -> int: ...

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...

    async def hset(self, key: str, mapping: dict[str, str]) -> int: ...

    async def hget(self, key: str, field: str) -> str | None: ...

    async def hgetall(self, key: str) -> dict[str, str]: ...

    async def hdel(self, key: str, *fields: str) -> int: ...

    async def sadd(self, key: str, *members: str) -> int: ...

    async def srem(self, key: str, *members: str) -> int: ...

    async def smembers(self, key: str) -> StrSet: ...

    async def delete(self, *keys: str) -> int: ...

    async def keys(self, pattern: str) -> list[str]: ...


class RedisStreamAdapter:
    """Adapts a ``redis.asyncio.Redis`` (``decode_responses=True``) to :class:`StreamRedis`.

    The underlying client is typed ``Any`` for the same reason
    :mod:`app.redis.client` does it: the runtime ``redis`` and the ``types-redis``
    stubs drift. This module's public surface stays fully typed.
    """

    def __init__(self, redis: Any) -> None:
        self._r: Any = redis

    @classmethod
    def from_url(cls, url: str) -> RedisStreamAdapter:
        """Build an adapter from a Redis URL (``decode_responses=True``)."""
        from redis.asyncio import Redis

        return cls(Redis.from_url(url, decode_responses=True))

    async def xadd(self, key: str, fields: dict[str, str], *, entry_id: str = "*") -> str:
        return str(await self._r.xadd(key, fields, id=entry_id))

    async def xrange(
        self, key: str, start: str = "-", end: str = "+", *, count: int | None = None
    ) -> list[StreamEntry]:
        raw = await self._r.xrange(key, min=start, max=end, count=count)
        return [(str(eid), {str(k): str(v) for k, v in fields.items()}) for eid, fields in raw]

    async def xlen(self, key: str) -> int:
        return int(await self._r.xlen(key))

    async def xtrim_minid(self, key: str, min_id: str) -> int:
        # approximate=False forces exact trimming; the default (True) only trims at
        # whole macro-node boundaries and is a no-op for small streams.
        return int(await self._r.xtrim(key, minid=min_id, approximate=False))

    async def incr(self, key: str, amount: int = 1) -> int:
        return int(await self._r.incrby(key, amount))

    async def get(self, key: str) -> str | None:
        value = await self._r.get(key)
        return None if value is None else str(value)

    async def set(self, key: str, value: str) -> None:
        await self._r.set(key, value)

    async def hset(self, key: str, mapping: dict[str, str]) -> int:
        if not mapping:
            return 0
        return int(await self._r.hset(key, mapping=mapping))

    async def hget(self, key: str, field: str) -> str | None:
        value = await self._r.hget(key, field)
        return None if value is None else str(value)

    async def hgetall(self, key: str) -> dict[str, str]:
        raw = await self._r.hgetall(key)
        return {str(k): str(v) for k, v in raw.items()}

    async def hdel(self, key: str, *fields: str) -> int:
        if not fields:
            return 0
        return int(await self._r.hdel(key, *fields))

    async def sadd(self, key: str, *members: str) -> int:
        if not members:
            return 0
        return int(await self._r.sadd(key, *members))

    async def srem(self, key: str, *members: str) -> int:
        if not members:
            return 0
        return int(await self._r.srem(key, *members))

    async def smembers(self, key: str) -> StrSet:
        return {str(m) for m in await self._r.smembers(key)}

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        return int(await self._r.delete(*keys))

    async def keys(self, pattern: str) -> list[str]:
        return [str(k) for k in await self._r.keys(pattern)]

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._r.aclose()


class FakeStreamRedis:
    """An in-process double of :class:`StreamRedis` (streams + hashes + strings + sets).

    Models Redis Stream ids as ``"<seq>-0"`` monotonic ints so ``xrange`` ordering
    and ``xtrim minid`` semantics match a real server closely enough for the
    broker's logic. Not a general emulator — unsupported commands raise.
    """

    def __init__(self) -> None:
        self._streams: dict[str, list[StreamEntry]] = {}
        self._stream_seq: dict[str, int] = {}
        self._strings: dict[str, str] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._sets: dict[str, set[str]] = {}

    @staticmethod
    def _parse_id(entry_id: str) -> tuple[int, int]:
        ms, _, seq = entry_id.partition("-")
        return int(ms), int(seq or 0)

    async def xadd(self, key: str, fields: dict[str, str], *, entry_id: str = "*") -> str:
        stream = self._streams.setdefault(key, [])
        if entry_id == "*":
            seq = self._stream_seq.get(key, 0) + 1
            self._stream_seq[key] = seq
            new_id = f"{seq}-0"
        else:
            new_id = entry_id
            ms, _ = self._parse_id(entry_id)
            self._stream_seq[key] = max(self._stream_seq.get(key, 0), ms)
        stream.append((new_id, dict(fields)))
        return new_id

    async def xrange(
        self, key: str, start: str = "-", end: str = "+", *, count: int | None = None
    ) -> list[StreamEntry]:
        stream = self._streams.get(key, [])
        lo = (float("-inf"), float("-inf")) if start == "-" else self._parse_id(start)
        hi = (float("inf"), float("inf")) if end == "+" else self._parse_id(end)
        out = [(eid, dict(f)) for eid, f in stream if lo <= self._parse_id(eid) <= hi]
        out.sort(key=lambda e: self._parse_id(e[0]))
        return out if count is None else out[:count]

    async def xlen(self, key: str) -> int:
        return len(self._streams.get(key, []))

    async def xtrim_minid(self, key: str, min_id: str) -> int:
        stream = self._streams.get(key)
        if not stream:
            return 0
        floor = self._parse_id(min_id)
        kept = [(eid, f) for eid, f in stream if self._parse_id(eid) >= floor]
        removed = len(stream) - len(kept)
        self._streams[key] = kept
        return removed

    async def incr(self, key: str, amount: int = 1) -> int:
        value = int(self._strings.get(key, "0")) + amount
        self._strings[key] = str(value)
        return value

    async def get(self, key: str) -> str | None:
        return self._strings.get(key)

    async def set(self, key: str, value: str) -> None:
        self._strings[key] = value

    async def hset(self, key: str, mapping: dict[str, str]) -> int:
        h = self._hashes.setdefault(key, {})
        added = sum(1 for f in mapping if f not in h)
        h.update(mapping)
        return added

    async def hget(self, key: str, field: str) -> str | None:
        return self._hashes.get(key, {}).get(field)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))

    async def hdel(self, key: str, *fields: str) -> int:
        h = self._hashes.get(key)
        if not h:
            return 0
        removed = 0
        for f in fields:
            if h.pop(f, None) is not None:
                removed += 1
        return removed

    async def sadd(self, key: str, *members: str) -> int:
        s = self._sets.setdefault(key, set())
        added = sum(1 for m in members if m not in s)
        s.update(members)
        return added

    async def srem(self, key: str, *members: str) -> int:
        s = self._sets.get(key)
        if not s:
            return 0
        removed = sum(1 for m in members if m in s)
        s.difference_update(members)
        return removed

    async def smembers(self, key: str) -> StrSet:
        return set(self._sets.get(key, set()))

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            for store in (self._streams, self._strings, self._hashes, self._sets):
                if key in store:
                    del store[key]
                    removed += 1
        return removed

    async def keys(self, pattern: str) -> list[str]:
        everything = (
            set(self._streams)
            | set(self._strings)
            | set(self._hashes)
            | set(self._sets)
        )
        return [k for k in everything if fnmatch.fnmatch(k, pattern)]
