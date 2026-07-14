"""The object-store clip tier — durable, cross-process, cross-fleet clip reuse.

The L1 (in-process) and L2 (Redis) tiers are *fast* but *ephemeral relative to a
clip's lifetime*: a process restart empties L1, and L2 entries expire. The clip
bytes, however, live forever in object storage (the render pipeline persists them
there because provider task URLs expire — see AGENTS.md). This module makes
object storage itself the **L3 cache tier**: a tiny JSON *sidecar* per render key
records the :class:`~app.cache.clips.record.ClipRecord`, so even a cold fleet that
has never seen a render key can discover that the clip already exists and serve it
for zero video-seconds.

Two seams:

* :class:`ClipBlobStore` — the minimal slice of the object store this tier needs
  (put/get/exists/delete bytes + presigned URLs). The production
  :class:`app.storage.object_store.ObjectStore` satisfies it structurally; tests
  use :class:`InMemoryClipStore` (a deterministic dict-backed fake, no network).
* :class:`ObjectStoreCacheBackend` — a :class:`~app.cache.interface.CacheBackend`
  whose storage *is* the object store. It serialises a :class:`ClipRecord` to a
  sidecar key and enforces TTL with a stored expiry timestamp (object stores have
  no native per-object TTL we can rely on cross-provider, so we carry it inline).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from app.cache.clock import SYSTEM_CLOCK, Clock
from app.cache.entry import CacheEntry
from app.cache.errors import CacheBackendError
from app.cache.interface import CacheBackend

#: Object-key prefix for clip-cache record sidecars (distinct from ``clips/`` so
#: the dedup layer never collides with the §8.7 per-shot ``clips/<book>/<shot>``).
SIDECAR_PREFIX = "clipcache/records"


@runtime_checkable
class ClipBlobStore(Protocol):
    """The slice of the object store the clip tier needs.

    Structurally satisfied by :class:`app.storage.object_store.ObjectStore`
    (which also satisfies :class:`app.memory.interfaces.BlobStore`).
    """

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None: ...

    def get_bytes(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def presigned_get_url(self, key: str, ttl: int = 3600) -> str: ...


class InMemoryClipStore:
    """A deterministic, dict-backed :class:`ClipBlobStore` for tests (no network).

    Records every put/get/delete so tests can assert the object tier was actually
    used. URLs are synthetic but stable (``memory://<bucket>/<key>``).
    """

    def __init__(self, *, bucket: str = "kinora-test") -> None:
        self._bucket = bucket
        self._objects: dict[str, bytes] = {}
        self.put_calls = 0
        self.get_calls = 0
        self.delete_calls = 0

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.put_calls += 1
        self._objects[key] = bytes(data)

    def get_bytes(self, key: str) -> bytes:
        self.get_calls += 1
        try:
            return self._objects[key]
        except KeyError as exc:  # mimic a not-found surfacing as an error
            raise FileNotFoundError(key) from exc

    def exists(self, key: str) -> bool:
        return key in self._objects

    def delete(self, key: str) -> None:
        self.delete_calls += 1
        self._objects.pop(key, None)

    def presigned_get_url(self, key: str, ttl: int = 3600) -> str:
        return f"memory://{self._bucket}/{key}"

    # --- test introspection --- #

    def keys(self) -> list[str]:
        return sorted(self._objects.keys())

    def size(self) -> int:
        return len(self._objects)


class ObjectStoreCacheBackend(CacheBackend):
    """A :class:`CacheBackend` persisting cache entries as object-store sidecars.

    Each entry is one small JSON object at ``<prefix>/<key>.json`` containing the
    serialised value plus expiry/tag metadata. TTL is enforced in-band: a read
    that finds an expired sidecar deletes it and reports a miss. Tag deletion is
    best-effort via a per-tag index sidecar (object stores have no secondary
    index), kept small because clip tags are coarse (per book / per entity).

    The value stored must be JSON-serialisable (a :class:`ClipRecord` dumped to a
    dict). The backend is value-agnostic — serialisation is the caller's job via
    the facade's codec — so it just round-trips the already-encoded payload.
    """

    name = "object"

    def __init__(
        self,
        store: ClipBlobStore,
        *,
        prefix: str = SIDECAR_PREFIX,
        clock: Clock | None = None,
    ) -> None:
        self._store = store
        self._prefix = prefix.rstrip("/")
        self._clock = clock or SYSTEM_CLOCK

    def _sidecar_key(self, key: str) -> str:
        # Keys are already content-addressed hex+separators; encode the separator
        # so the object key is path-safe.
        safe = key.replace(":", "_").replace("/", "_")
        return f"{self._prefix}/{safe}.json"

    def _tag_index_key(self, tag: str) -> str:
        safe = tag.replace(":", "_").replace("/", "_").replace("#", "_")
        return f"{self._prefix}/tags/{safe}.json"

    def _encode(self, entry: CacheEntry) -> bytes:
        envelope = {
            "value": entry.value,
            "created_at": entry.created_at,
            "expires_at": entry.expires_at,
            "ttl": entry.ttl,
            "tags": sorted(entry.tags),
            "negative": entry.negative,
        }
        return json.dumps(envelope, separators=(",", ":"), ensure_ascii=True).encode("utf-8")

    def _decode(self, raw: bytes) -> CacheEntry:
        env = json.loads(raw.decode("utf-8"))
        return CacheEntry(
            value=env.get("value"),
            created_at=float(env.get("created_at", 0.0)),
            expires_at=env.get("expires_at"),
            ttl=env.get("ttl"),
            tags=frozenset(env.get("tags", [])),
            negative=bool(env.get("negative", False)),
        )

    async def get(self, key: str) -> CacheEntry | None:
        sidecar = self._sidecar_key(key)
        try:
            if not self._store.exists(sidecar):
                return None
            raw = self._store.get_bytes(sidecar)
        except CacheBackendError:
            raise
        except Exception as exc:  # noqa: BLE001 - wrap any transport/IO error
            raise CacheBackendError(f"object get failed: {key}") from exc
        try:
            entry = self._decode(raw)
        except (ValueError, KeyError) as exc:
            raise CacheBackendError(f"object decode failed: {key}") from exc
        if entry.is_expired(self._clock.time()):
            # Lazily reap an expired sidecar so the next read is a clean miss.
            with contextlib.suppress(Exception):
                self._store.delete(sidecar)
            return None
        return entry

    async def set(self, key: str, entry: CacheEntry) -> None:
        sidecar = self._sidecar_key(key)
        try:
            self._store.put_bytes(sidecar, self._encode(entry), content_type="application/json")
        except Exception as exc:  # noqa: BLE001
            raise CacheBackendError(f"object set failed: {key}") from exc
        if entry.tags:
            await self._index_tags(key, entry.tags)

    async def _index_tags(self, key: str, tags: frozenset[str]) -> None:
        for tag in tags:
            idx_key = self._tag_index_key(tag)
            members: set[str] = set()
            try:
                if self._store.exists(idx_key):
                    members = set(json.loads(self._store.get_bytes(idx_key).decode("utf-8")))
            except Exception:  # noqa: BLE001 - a corrupt index just rebuilds
                members = set()
            if key in members:
                continue
            members.add(key)
            try:
                self._store.put_bytes(
                    idx_key,
                    json.dumps(sorted(members), ensure_ascii=True).encode("utf-8"),
                    content_type="application/json",
                )
            except Exception as exc:  # noqa: BLE001
                raise CacheBackendError(f"object tag index failed: {tag}") from exc

    async def delete(self, key: str) -> bool:
        sidecar = self._sidecar_key(key)
        try:
            existed = self._store.exists(sidecar)
            if existed:
                self._store.delete(sidecar)
        except Exception as exc:  # noqa: BLE001
            raise CacheBackendError(f"object delete failed: {key}") from exc
        return existed

    async def delete_many(self, keys: Iterable[str]) -> int:
        removed = 0
        for key in keys:
            if await self.delete(key):
                removed += 1
        return removed

    async def clear(self) -> None:
        # An object store has no cheap "clear prefix"; clearing is intentionally a
        # no-op here (the durable tier is authoritative and meant to persist). The
        # facade's invalidate_namespace therefore only clears the faster tiers.
        return None

    async def delete_tag(self, tag: str) -> int:
        idx_key = self._tag_index_key(tag)
        try:
            if not self._store.exists(idx_key):
                return 0
            members = json.loads(self._store.get_bytes(idx_key).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise CacheBackendError(f"object tag read failed: {tag}") from exc
        removed = 0
        for key in members:
            if await self.delete(key):
                removed += 1
        with contextlib.suppress(Exception):
            self._store.delete(idx_key)
        return removed

    async def health(self) -> bool:
        # The store is considered healthy if a probe doesn't raise; a missing
        # probe object is fine (it just doesn't exist).
        try:
            self._store.exists(f"{self._prefix}/.health")
        except Exception:  # noqa: BLE001
            return False
        return True


__all__ = [
    "SIDECAR_PREFIX",
    "ClipBlobStore",
    "InMemoryClipStore",
    "ObjectStoreCacheBackend",
]
