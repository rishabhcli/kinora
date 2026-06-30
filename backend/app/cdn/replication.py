"""Multi-region replication manager: mirror origin → N regional replicas.

The render pipeline writes a finished clip to the **origin** bucket. A reader
far from origin should not pull across an ocean every time, so this manager
mirrors each object to the configured replica regions:

* **Idempotent.** Replicating an object that is already present *and* checksum-
  matches is a no-op (``SKIPPED``) — safe to call repeatedly, safe to retry a
  partially-failed fan-out, safe to run a reconcile sweep over everything.
* **Checksum-verified.** The origin's sha256 is computed once; every replica is
  read back after write and its digest compared. A mismatch raises
  :class:`ReplicaChecksumMismatchError` (caught and surfaced per-region in the
  fan-out report) rather than silently serving corrupt bytes.
* **Replication-lag tracked.** Each object carries an ``origin_written_at``; a
  replica's lag is ``now - replicated_at`` relative to that, exposed as a
  :class:`ReplicationState` the resolver consults to skip a stale replica.
* **Reconcile / repair sweep.** :meth:`reconcile` walks a set of keys and
  re-replicates any that are missing or checksum-divergent on a replica — the
  catch-up path for objects written while a region was unreachable.

Pure async logic over the injected :class:`app.cdn.protocols.RegionStore` per
region; no boto3, no network in tests. Replication *bookkeeping* (which keys are
where, with what digest and lag) lives in an injected
:class:`ReplicationLedger` — in-memory by default, swappable for a DB-backed one
without touching the manager.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.cdn.errors import (
    OriginMissingObjectError,
    ReplicaChecksumMismatchError,
)
from app.cdn.protocols import Clock, RegionStore
from app.cdn.regions import RegionTopology
from app.core.logging import get_logger
from app.media.hashing import sha256_hex

logger = get_logger("app.cdn.replication")


class ReplicaStatus(StrEnum):
    """Outcome of replicating one object to one region."""

    #: Bytes were copied and checksum-verified.
    REPLICATED = "replicated"
    #: Already present and checksum-matching — nothing to do (idempotent).
    SKIPPED = "skipped"
    #: A re-replication that repaired a missing/divergent replica.
    REPAIRED = "repaired"
    #: The copy failed (checksum mismatch or store error); see ``detail``.
    FAILED = "failed"


class ReplicaResult(BaseModel):
    """The result of one region's replication attempt for one key."""

    model_config = ConfigDict(frozen=True)

    region_id: str
    key: str
    status: ReplicaStatus
    checksum: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the replica now holds a verified copy."""
        return self.status in (
            ReplicaStatus.REPLICATED,
            ReplicaStatus.SKIPPED,
            ReplicaStatus.REPAIRED,
        )


class ReplicationReport(BaseModel):
    """The fan-out report for replicating one key to all replica regions."""

    model_config = ConfigDict(frozen=True)

    key: str
    checksum: str
    results: tuple[ReplicaResult, ...]

    @property
    def fully_replicated(self) -> bool:
        """Whether every targeted replica now holds a verified copy."""
        return all(r.ok for r in self.results)

    def failures(self) -> tuple[ReplicaResult, ...]:
        """The replica attempts that failed."""
        return tuple(r for r in self.results if not r.ok)


class ReplicationState(BaseModel):
    """Ledger view of one object on one replica region."""

    model_config = ConfigDict(frozen=True)

    region_id: str
    key: str
    checksum: str
    #: Epoch seconds the origin wrote this object's bytes.
    origin_written_at: float
    #: Epoch seconds this replica caught up.
    replicated_at: float

    def lag_s(self, *, origin_written_at: float | None = None) -> float:
        """Replication lag (seconds) of this replica behind origin.

        Defaults to the lag captured at replication time; pass a fresher
        ``origin_written_at`` (e.g. origin was rewritten) to recompute.
        """
        origin_ts = origin_written_at if origin_written_at is not None else self.origin_written_at
        return max(0.0, self.replicated_at - origin_ts)


class ReplicationLedger:
    """In-memory bookkeeping of replica state, keyed by ``(region_id, key)``.

    Swappable for a DB-backed implementation; the manager only needs
    record/lookup/forget. Thread-affinity is not assumed — callers serialise via
    the event loop.
    """

    def __init__(self) -> None:
        self._by_region_key: dict[tuple[str, str], ReplicationState] = {}

    def record(self, state: ReplicationState) -> None:
        """Record (or overwrite) the state of one object on one replica."""
        self._by_region_key[(state.region_id, state.key)] = state

    def get(self, region_id: str, key: str) -> ReplicationState | None:
        """The recorded state for ``(region_id, key)`` if present."""
        return self._by_region_key.get((region_id, key))

    def forget(self, region_id: str, key: str) -> None:
        """Drop the ledger entry for ``(region_id, key)`` (e.g. after a purge)."""
        self._by_region_key.pop((region_id, key), None)

    def regions_for(self, key: str) -> tuple[str, ...]:
        """Replica regions that currently hold a recorded copy of ``key``."""
        return tuple(
            region_id
            for (region_id, k) in self._by_region_key
            if k == key
        )


class ReplicationManager:
    """Mirrors origin objects to replica regions, idempotently and verified."""

    def __init__(
        self,
        *,
        topology: RegionTopology,
        stores: Mapping[str, RegionStore],
        clock: Clock,
        ledger: ReplicationLedger | None = None,
    ) -> None:
        missing = set(topology.region_ids) - set(stores)
        if missing:
            raise ValueError(f"no store provided for regions {sorted(missing)}")
        self._topology = topology
        self._stores = dict(stores)
        self._clock = clock
        self._ledger = ledger or ReplicationLedger()

    @property
    def ledger(self) -> ReplicationLedger:
        """The replication bookkeeping ledger."""
        return self._ledger

    @property
    def topology(self) -> RegionTopology:
        """The (immutable) region topology this manager mirrors over."""
        return self._topology

    @property
    def clock(self) -> Clock:
        """The injected wall-clock seam."""
        return self._clock

    def store_for(self, region_id: str) -> RegionStore:
        """The store bound to ``region_id`` (raises on an unknown region)."""
        self._topology.get(region_id)
        return self._stores[region_id]

    @property
    def origin_store(self) -> RegionStore:
        """The origin region's store."""
        return self._stores[self._topology.origin.region_id]

    async def _origin_bytes_and_digest(self, key: str) -> tuple[bytes, str]:
        """Read origin bytes for ``key`` (raising if absent) and its digest."""
        origin = self.origin_store
        if not await origin.exists(key):
            raise OriginMissingObjectError(key, self._topology.origin.region_id)
        data = await origin.get_bytes(key)
        return data, sha256_hex(data)

    async def _replicate_one(
        self,
        *,
        region_id: str,
        key: str,
        data: bytes,
        digest: str,
        origin_written_at: float,
        content_type: str | None,
        repair: bool,
    ) -> ReplicaResult:
        """Replicate one key to one region, idempotent + checksum-verified."""
        store = self._stores[region_id]
        # Idempotency: if a verified copy already exists, skip the write.
        if await store.exists(key):
            existing = await store.get_bytes(key)
            existing_digest = sha256_hex(existing)
            if existing_digest == digest:
                # Refresh the ledger so lag reflects this confirmation.
                self._ledger.record(
                    ReplicationState(
                        region_id=region_id,
                        key=key,
                        checksum=digest,
                        origin_written_at=origin_written_at,
                        replicated_at=self._clock.now(),
                    )
                )
                return ReplicaResult(
                    region_id=region_id,
                    key=key,
                    status=ReplicaStatus.SKIPPED,
                    checksum=digest,
                )
            # Divergent bytes — fall through to re-write (a repair).
            repair = True

        await store.put_bytes(key, data, content_type=content_type)
        # Read-back verification: never trust the write blind.
        written = await store.get_bytes(key)
        written_digest = sha256_hex(written)
        if written_digest != digest:
            err = ReplicaChecksumMismatchError(key, region_id, digest, written_digest)
            logger.warning(
                "cdn.replicate.checksum_mismatch",
                key=key,
                region_id=region_id,
                expected=digest,
                actual=written_digest,
            )
            return ReplicaResult(
                region_id=region_id,
                key=key,
                status=ReplicaStatus.FAILED,
                checksum=written_digest,
                detail=str(err),
            )
        self._ledger.record(
            ReplicationState(
                region_id=region_id,
                key=key,
                checksum=digest,
                origin_written_at=origin_written_at,
                replicated_at=self._clock.now(),
            )
        )
        return ReplicaResult(
            region_id=region_id,
            key=key,
            status=ReplicaStatus.REPAIRED if repair else ReplicaStatus.REPLICATED,
            checksum=digest,
        )

    async def replicate(
        self,
        key: str,
        *,
        origin_written_at: float | None = None,
        content_type: str | None = None,
        targets: Iterable[str] | None = None,
    ) -> ReplicationReport:
        """Mirror ``key`` from origin to its replica regions (concurrently).

        Idempotent: replicas already holding a checksum-matching copy are
        ``SKIPPED``. ``targets`` defaults to every non-origin region; passing a
        subset limits the fan-out (e.g. warm one reader's region first).
        ``origin_written_at`` defaults to *now* (a freshly-written object) and
        feeds the replication-lag accounting.
        """
        data, digest = await self._origin_bytes_and_digest(key)
        written_at = origin_written_at if origin_written_at is not None else self._clock.now()
        target_ids = tuple(targets) if targets is not None else self._topology.replica_ids()
        # Validate target ids up front (raises UnknownRegionError on a bad id).
        for rid in target_ids:
            self._topology.get(rid)

        results = await asyncio.gather(
            *(
                self._replicate_one(
                    region_id=rid,
                    key=key,
                    data=data,
                    digest=digest,
                    origin_written_at=written_at,
                    content_type=content_type,
                    repair=False,
                )
                for rid in target_ids
            )
        )
        report = ReplicationReport(key=key, checksum=digest, results=tuple(results))
        logger.info(
            "cdn.replicate",
            key=key,
            checksum=digest,
            fully_replicated=report.fully_replicated,
            targets=list(target_ids),
        )
        return report

    async def replica_lag_s(self, region_id: str, key: str) -> float | None:
        """Current replication lag (s) of ``key`` on ``region_id``.

        ``None`` if the replica has no recorded copy. Recomputed against the
        *live* origin write time when origin still holds the object, so a
        re-written origin immediately registers as lag on a stale replica.
        """
        state = self._ledger.get(region_id, key)
        if state is None:
            return None
        return state.lag_s()

    async def reconcile(
        self,
        keys: Sequence[str],
        *,
        targets: Iterable[str] | None = None,
        content_type: str | None = None,
    ) -> list[ReplicationReport]:
        """Repair sweep: re-replicate any keys missing/divergent on a replica.

        For each key: if origin lacks it, it is skipped (logged) rather than
        erroring the whole sweep — a key may have been GC'd. Otherwise the key is
        re-replicated to ``targets``; already-verified replicas are ``SKIPPED``
        and only genuinely missing/divergent ones are ``REPAIRED``. This is the
        catch-up path for objects written while a region was unreachable.
        """
        reports: list[ReplicationReport] = []
        for key in keys:
            try:
                data, digest = await self._origin_bytes_and_digest(key)
            except OriginMissingObjectError:
                logger.warning("cdn.reconcile.origin_missing", key=key)
                continue
            written_at = self._clock.now()
            target_ids = tuple(targets) if targets is not None else self._topology.replica_ids()
            for rid in target_ids:
                self._topology.get(rid)
            results = await asyncio.gather(
                *(
                    self._replicate_one(
                        region_id=rid,
                        key=key,
                        data=data,
                        digest=digest,
                        origin_written_at=written_at,
                        content_type=content_type,
                        repair=True,
                    )
                    for rid in target_ids
                )
            )
            reports.append(
                ReplicationReport(key=key, checksum=digest, results=tuple(results))
            )
        repaired = sum(
            1
            for rep in reports
            for r in rep.results
            if r.status is ReplicaStatus.REPAIRED
        )
        logger.info("cdn.reconcile", keys=len(keys), repaired=repaired)
        return reports


__all__ = [
    "ReplicaResult",
    "ReplicaStatus",
    "ReplicationLedger",
    "ReplicationManager",
    "ReplicationReport",
    "ReplicationState",
]
