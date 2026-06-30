"""Adapters binding the existing synchronous object store to the async seams.

The real :class:`app.storage.object_store.ObjectStore` is a synchronous boto3
wrapper. :class:`ObjectStoreRegion` wraps one such client as an async
:class:`app.cdn.protocols.RegionStore` by running the blocking calls in the
default thread executor (:func:`asyncio.to_thread`), so the existing object-store
code is used unchanged — the CDN layer never touches boto3 directly.

:class:`SystemClock` is the production :class:`app.cdn.protocols.Clock`.

This module is imported lazily by the composition root (it pulls in boto3 via
the object store), keeping ``import app.cdn`` cheap and infra-free.
"""

from __future__ import annotations

import asyncio
import time

from app.storage.object_store import ObjectStore

# Object-store error codes that mean "not found" (mirrors object_store._NOT_FOUND_CODES);
# duplicated here to avoid importing a private symbol.
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NoSuchBucket", "NotFound"})


class SystemClock:
    """The production wall clock (epoch seconds)."""

    def now(self) -> float:
        return time.time()


class ObjectStoreRegion:
    """Async :class:`~app.cdn.protocols.RegionStore` over a sync ``ObjectStore``."""

    def __init__(self, region_id: str, store: ObjectStore) -> None:
        self._region_id = region_id
        self._store = store

    @property
    def region_id(self) -> str:
        return self._region_id

    @property
    def object_store(self) -> ObjectStore:
        """The wrapped synchronous object store."""
        return self._store

    async def put_bytes(
        self, key: str, data: bytes, content_type: str | None = None
    ) -> None:
        await asyncio.to_thread(self._store.put_bytes, key, data, content_type)

    async def get_bytes(self, key: str) -> bytes:
        return await asyncio.to_thread(self._store.get_bytes, key)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._store.exists, key)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._store.delete, key)

    async def size(self, key: str) -> int | None:
        def _size() -> int | None:
            from botocore.exceptions import ClientError

            try:
                resp = self._store._client.head_object(  # noqa: SLF001 - boto3 head
                    Bucket=self._store.bucket, Key=key
                )
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code in _NOT_FOUND_CODES:
                    return None
                raise
            length = resp.get("ContentLength")
            return int(length) if length is not None else None

        return await asyncio.to_thread(_size)

    def presigned_get_url(self, key: str, ttl: int = 3600) -> str:
        return self._store.presigned_get_url(key, ttl=ttl)

    def public_url(self, key: str) -> str | None:
        return self._store.public_url(key)


__all__ = ["ObjectStoreRegion", "SystemClock"]
