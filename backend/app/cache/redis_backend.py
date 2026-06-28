"""L2 — the Redis-backed cache backend.

Stores each :class:`~app.cache.entry.CacheEntry` as a single Redis string whose
value is a small self-describing envelope (codec name + flags + metadata +
codec'd payload), with a native Redis TTL (``PX``) mirroring the entry's
``expires_at`` so Redis itself reaps expired keys. Tag membership is tracked in
Redis sets (``{prefix}:tag:{tag}`` -> member keys) so :meth:`delete_tag` can drop
a whole tag with one ``SMEMBERS`` + pipelined ``DEL``.

Design choices:

* **Binary-safe.** We open a *separate* connection with ``decode_responses=False``
  so codec payloads (which may be ``pickle`` bytes) round-trip intact, regardless
  of how the shared :class:`~app.redis.client.RedisClient` was configured. The
  envelope header is JSON (ASCII) and the payload follows after a length prefix.
* **Fail-open option.** Transport errors are wrapped as
  :class:`~app.cache.errors.CacheBackendError`; the facade decides whether to
  treat them as a soft miss.
* **Key scoping.** Every key is prefixed (default ``kinora:cache``) so a flush /
  clear only touches this layer's keys, never the render queue or pubsub keys.

The envelope wire format (all big-endian, after a 1-byte version)::

    version(1) | header_len(4) | header_json(header_len) | payload(rest)

``header_json`` carries ``{codec, neg, ca, ea, ttl, tags}``; ``payload`` is the
codec output (empty for negative entries).
"""

from __future__ import annotations

import json
import struct
from collections.abc import Iterable
from typing import Any

from app.cache.clock import SYSTEM_CLOCK, Clock
from app.cache.codecs import DEFAULT_CODEC, Codec
from app.cache.entry import CacheEntry
from app.cache.errors import CacheBackendError, SerializationError
from app.cache.interface import CacheBackend

_WIRE_VERSION = 1
_HEADER_STRUCT = struct.Struct(">BI")  # version (B) + header length (I)


def encode_envelope(entry: CacheEntry, codec: Codec) -> bytes:
    """Serialize a :class:`CacheEntry` into the on-wire envelope bytes."""
    if entry.negative:
        payload = b""
        codec_name = codec.name
    else:
        payload = codec.encode(entry.value)
        codec_name = codec.name
    header = {
        "codec": codec_name,
        "neg": entry.negative,
        "ca": entry.created_at,
        "ea": entry.expires_at,
        "ttl": entry.ttl,
        "tags": sorted(entry.tags),
    }
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return _HEADER_STRUCT.pack(_WIRE_VERSION, len(header_bytes)) + header_bytes + payload


def decode_envelope(raw: bytes, codec: Codec) -> CacheEntry:
    """Inverse of :func:`encode_envelope` (raises :class:`SerializationError`)."""
    if len(raw) < _HEADER_STRUCT.size:
        raise SerializationError("cache envelope too short")
    version, header_len = _HEADER_STRUCT.unpack(raw[: _HEADER_STRUCT.size])
    if version != _WIRE_VERSION:
        raise SerializationError(f"unknown cache envelope version {version}")
    start = _HEADER_STRUCT.size
    end = start + header_len
    try:
        header = json.loads(raw[start:end].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SerializationError(f"bad cache envelope header: {exc}") from exc
    payload = raw[end:]
    negative = bool(header.get("neg", False))
    value: Any = None if negative else codec.decode(payload)
    return CacheEntry(
        value=value,
        created_at=float(header["ca"]),
        expires_at=None if header.get("ea") is None else float(header["ea"]),
        ttl=None if header.get("ttl") is None else float(header["ttl"]),
        tags=frozenset(header.get("tags", ())),
        negative=negative,
        codec=str(header.get("codec")) if header.get("codec") is not None else None,
    )


class RedisCache(CacheBackend):
    """A Redis string-per-entry backend with tag sets and native TTL.

    Args:
        redis: A binary-mode ``redis.asyncio.Redis`` (``decode_responses=False``).
        prefix: Key prefix scoping this backend's keys.
        codec: Codec used for value payloads (default JSON).
        clock: Time source for TTL math (Redis owns the real expiry; the clock is
            only used to convert ``expires_at`` -> remaining ms on write).
    """

    name = "redis"

    def __init__(
        self,
        redis: Any,
        *,
        prefix: str = "kinora:cache",
        codec: Codec | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._redis = redis
        self._prefix = prefix.rstrip(":")
        self._codec = codec or DEFAULT_CODEC
        self._clock = clock or SYSTEM_CLOCK

    # --- key helpers --- #

    def _k(self, key: str) -> str:
        return f"{self._prefix}:k:{key}"

    def _tag_key(self, tag: str) -> str:
        return f"{self._prefix}:tag:{tag}"

    # --- CacheBackend --- #

    async def get(self, key: str) -> CacheEntry | None:
        try:
            raw = await self._redis.get(self._k(key))
        except Exception as exc:  # noqa: BLE001 - normalize any driver error
            raise CacheBackendError(f"redis get failed: {exc}") from exc
        if raw is None:
            return None
        if isinstance(raw, str):  # client misconfigured with decode_responses
            raw = raw.encode("utf-8")
        return decode_envelope(raw, self._codec)

    async def set(self, key: str, entry: CacheEntry) -> None:
        blob = encode_envelope(entry, self._codec)
        px = None
        if entry.expires_at is not None:
            remaining_ms = int(max(0.0, entry.expires_at - self._clock.time()) * 1000)
            # A 0ms PX would be rejected; store at least 1ms so the write lands.
            px = max(1, remaining_ms)
        try:
            if px is None:
                await self._redis.set(self._k(key), blob)
            else:
                await self._redis.set(self._k(key), blob, px=px)
            if entry.tags:
                pipe = self._redis.pipeline()
                for tag in entry.tags:
                    pipe.sadd(self._tag_key(tag), key)
                await pipe.execute()
        except CacheBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CacheBackendError(f"redis set failed: {exc}") from exc

    async def delete(self, key: str) -> bool:
        try:
            removed = await self._redis.delete(self._k(key))
        except Exception as exc:  # noqa: BLE001
            raise CacheBackendError(f"redis delete failed: {exc}") from exc
        return bool(removed)

    async def delete_many(self, keys: Iterable[str]) -> int:
        scoped = [self._k(k) for k in keys]
        if not scoped:
            return 0
        try:
            return int(await self._redis.delete(*scoped))
        except Exception as exc:  # noqa: BLE001
            raise CacheBackendError(f"redis delete_many failed: {exc}") from exc

    async def clear(self) -> None:
        """Drop every key under this backend's prefix (SCAN + pipelined DEL)."""
        pattern = f"{self._prefix}:*"
        try:
            cursor = 0
            while True:
                cursor, batch = await self._redis.scan(cursor=cursor, match=pattern, count=512)
                if batch:
                    await self._redis.delete(*batch)
                if cursor == 0:
                    break
        except Exception as exc:  # noqa: BLE001
            raise CacheBackendError(f"redis clear failed: {exc}") from exc

    async def delete_tag(self, tag: str) -> int:
        tag_key = self._tag_key(tag)
        try:
            members = await self._redis.smembers(tag_key)
            scoped = [self._k(_as_str(m)) for m in members]
            removed = 0
            if scoped:
                removed = int(await self._redis.delete(*scoped))
            await self._redis.delete(tag_key)
        except Exception as exc:  # noqa: BLE001
            raise CacheBackendError(f"redis delete_tag failed: {exc}") from exc
        return removed

    async def health(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:  # noqa: BLE001 - best-effort close
            return None


def _as_str(member: Any) -> str:
    """Redis set members may come back as bytes or str depending on the client."""
    if isinstance(member, bytes):
        return member.decode("utf-8")
    return str(member)


__all__ = ["RedisCache", "decode_envelope", "encode_envelope"]
