"""Deterministic in-memory fakes for the CDN layer (offline, no network/boto3).

Importable from app code (not just tests) so a local/dev composition can stand
up a multi-region CDN without infra. Everything is synchronous-under-the-hood
behind the async protocol surface, so tests stay exact and fast.

* :class:`FakeRegionStore` — an in-memory bucket implementing
  :class:`app.cdn.protocols.RegionStore`, with deliberate-corruption and
  drop-object hooks so the checksum / reconcile paths are exercisable.
* :class:`FakeCdnProvider` — an in-memory edge implementing
  :class:`app.cdn.protocols.CdnProvider`, tracking warmed/purged keys.
* :class:`FakeClock` — a manually-advanced clock implementing
  :class:`app.cdn.protocols.Clock`.
* :func:`demo_topology` — a three-region (na/eu/ap) topology for examples/tests.
"""

from __future__ import annotations

from app.cdn.regions import GeoPoint, Region, RegionTopology
from app.media.hashing import sha256_hex


class FakeClock:
    """A manually-advanced epoch clock (deterministic time for tests)."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._t = float(start)

    def now(self) -> float:
        """Current fake time (epoch seconds)."""
        return self._t

    def advance(self, seconds: float) -> float:
        """Advance the clock by ``seconds`` and return the new time."""
        self._t += float(seconds)
        return self._t


class FakeRegionStore:
    """An in-memory object store for one region (RegionStore protocol)."""

    def __init__(
        self,
        region_id: str,
        *,
        public_base_url: str | None = None,
    ) -> None:
        self._region_id = region_id
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self._objects: dict[str, bytes] = {}
        #: When set, every ``put_bytes`` stores *these* bytes instead of the
        #: written ones — simulating a silently-corrupting write so the
        #: replication read-back verification can be exercised.
        self.corrupt_on_put: bytes | None = None
        #: Observability counters for tests.
        self.puts = 0
        self.gets = 0

    @property
    def region_id(self) -> str:
        return self._region_id

    async def put_bytes(
        self, key: str, data: bytes, content_type: str | None = None
    ) -> None:
        self.puts += 1
        self._objects[key] = (
            bytes(self.corrupt_on_put) if self.corrupt_on_put is not None else bytes(data)
        )

    async def get_bytes(self, key: str) -> bytes:
        self.gets += 1
        try:
            return self._objects[key]
        except KeyError:
            raise KeyError(f"{self._region_id}:{key} not found") from None

    async def exists(self, key: str) -> bool:
        return key in self._objects

    async def delete(self, key: str) -> None:
        self._objects.pop(key, None)

    async def size(self, key: str) -> int | None:
        blob = self._objects.get(key)
        return None if blob is None else len(blob)

    def presigned_get_url(self, key: str, ttl: int = 3600) -> str:
        return f"https://{self._region_id}.fake-s3.local/{key}?X-Expires={ttl}"

    def public_url(self, key: str) -> str | None:
        if self._public_base_url is None:
            return None
        return f"{self._public_base_url}/{key}"

    # -- test hooks (not part of the protocol) ------------------------------- #

    def seed(self, key: str, data: bytes) -> None:
        """Synchronously place bytes (e.g. seed origin before replicating)."""
        self._objects[key] = bytes(data)

    def corrupt(self, key: str, data: bytes) -> None:
        """Overwrite ``key`` with divergent bytes (simulate bit-rot/tamper)."""
        self._objects[key] = bytes(data)

    def drop(self, key: str) -> None:
        """Remove ``key`` without going through the async delete (simulate loss)."""
        self._objects.pop(key, None)

    def digest(self, key: str) -> str | None:
        """The sha256 of ``key``'s bytes, or ``None`` if absent."""
        blob = self._objects.get(key)
        return None if blob is None else sha256_hex(blob)

    def keys(self) -> tuple[str, ...]:
        """All stored keys (sorted, for deterministic assertions)."""
        return tuple(sorted(self._objects))


class FakeCdnProvider:
    """An in-memory edge cache for one region (CdnProvider protocol)."""

    def __init__(self, region_id: str, *, fail_invalidate: bool = False) -> None:
        self._region_id = region_id
        self._cached: dict[str, str] = {}
        self._fail_invalidate = fail_invalidate
        #: Observability lists for tests.
        self.warmed: list[str] = []
        self.purged: list[str] = []

    @property
    def region_id(self) -> str:
        return self._region_id

    async def invalidate(self, key: str) -> None:
        if self._fail_invalidate:
            from app.cdn.errors import CachePurgeError

            raise CachePurgeError(key, "fake provider configured to fail")
        self._cached.pop(key, None)
        self.purged.append(key)

    async def warm(self, key: str, origin_url: str) -> None:
        self._cached[key] = origin_url
        self.warmed.append(key)

    async def is_cached(self, key: str) -> bool:
        return key in self._cached


def demo_topology() -> RegionTopology:
    """A three-region topology (na origin + eu/ap replicas) for tests/examples."""
    return RegionTopology(
        [
            Region(
                region_id="na",
                name="US East",
                location=GeoPoint(lat=39.0, lon=-77.5),
                continent="na",
                origin=True,
            ),
            Region(
                region_id="eu",
                name="EU West",
                location=GeoPoint(lat=53.3, lon=-6.3),
                continent="eu",
            ),
            Region(
                region_id="ap",
                name="AP Southeast",
                location=GeoPoint(lat=1.35, lon=103.8),
                continent="ap",
            ),
        ]
    )


__all__ = [
    "FakeCdnProvider",
    "FakeClock",
    "FakeRegionStore",
    "demo_topology",
]
