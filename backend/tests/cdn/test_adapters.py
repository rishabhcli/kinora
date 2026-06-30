"""Adapters: the sync ObjectStore surface wrapped as an async RegionStore.

Uses an in-memory stub mimicking the exact ``ObjectStore`` surface the adapter
calls (no boto3 / network), so the threaded delegation + ``size`` head logic is
exercised deterministically.
"""

from __future__ import annotations

from typing import Any

from app.cdn.adapters import ObjectStoreRegion, SystemClock
from app.cdn.protocols import RegionStore


class _FakeBoto:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self._objects:
            # Import the real ClientError so the adapter's except clause matches.
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        return {"ContentLength": len(self._objects[Key])}


class _FakeObjectStore:
    """Mimics app.storage.object_store.ObjectStore's surface (in-memory)."""

    def __init__(self, public_base_url: str | None = None) -> None:
        self._objects: dict[str, bytes] = {}
        self._public = public_base_url
        self._client = _FakeBoto(self._objects)

    @property
    def bucket(self) -> str:
        return "kinora"

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self._objects[key] = bytes(data)

    def get_bytes(self, key: str) -> bytes:
        return self._objects[key]

    def exists(self, key: str) -> bool:
        return key in self._objects

    def delete(self, key: str) -> None:
        self._objects.pop(key, None)

    def presigned_get_url(self, key: str, ttl: int = 3600) -> str:
        return f"https://s3/{key}?ttl={ttl}"

    def public_url(self, key: str) -> str | None:
        return f"{self._public}/{key}" if self._public else None


KEY = "clips/book1/shot_00001.mp4"


async def test_adapter_satisfies_protocol_and_roundtrips() -> None:
    region = ObjectStoreRegion("eu", _FakeObjectStore())  # type: ignore[arg-type]
    assert isinstance(region, RegionStore)
    assert region.region_id == "eu"

    assert await region.exists(KEY) is False
    assert await region.size(KEY) is None
    await region.put_bytes(KEY, b"hello", content_type="video/mp4")
    assert await region.exists(KEY) is True
    assert await region.get_bytes(KEY) == b"hello"
    assert await region.size(KEY) == 5
    assert region.presigned_get_url(KEY, ttl=60) == f"https://s3/{KEY}?ttl=60"
    assert region.public_url(KEY) is None
    await region.delete(KEY)
    assert await region.exists(KEY) is False


async def test_adapter_public_url() -> None:
    region = ObjectStoreRegion(
        "eu", _FakeObjectStore(public_base_url="https://cdn")  # type: ignore[arg-type]
    )
    region2 = ObjectStoreRegion("eu", _FakeObjectStore())  # type: ignore[arg-type]
    await region.put_bytes(KEY, b"x")
    assert region.public_url(KEY) == f"https://cdn/{KEY}"
    assert region2.public_url(KEY) is None


def test_system_clock_is_monotonic_seconds() -> None:
    clk = SystemClock()
    a = clk.now()
    b = clk.now()
    assert isinstance(a, float)
    assert b >= a
