"""Prefetch / warm hook: push the next-likely shots to the reader's region.

Kinora generates a few seconds ahead of the reader (the §-Scheduler buffer). The
same look-ahead drives *edge* warming: as the reader approaches a shot, ensure
its clip is (a) **replicated** to the reader's nearest region and (b) **warm**
in that region's edge cache — both *before* playback so the first byte is local.

Given the reader's current shot and the upcoming shot ids (from the Scheduler's
committed/speculative zone), this:

1. resolves the reader's nearest region from the hint (same scorer the resolver
   uses), then
2. for each upcoming key, **replicates** it to that one region (idempotent — a
   no-op if already there) and **warms** the edge (skipping keys the edge
   reports already cached).

Bounded by ``max_warm`` so a fast scroll doesn't fan out the whole book, and
fully best-effort: a per-key failure is recorded and the sweep continues (a cold
miss merely falls back to origin at play time, never blocks the reader).

Pure async logic over the manager + injected providers; no network in tests.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.cdn.protocols import CdnProvider
from app.cdn.regions import ReaderHint, RegionHealth
from app.cdn.replication import ReplicationManager
from app.cdn.signing import sign_url
from app.core.logging import get_logger

logger = get_logger("app.cdn.prefetch")

#: Default ceiling on how many upcoming shots to warm in one pass.
DEFAULT_MAX_WARM = 4


class PrefetchOutcome(StrEnum):
    """What happened to one key during a warm pass."""

    #: Replicated to the region and warmed into the edge.
    WARMED = "warmed"
    #: Already warm in the edge — nothing to do.
    ALREADY_WARM = "already_warm"
    #: Replicated but no edge provider for the region (replica-only warm).
    REPLICATED_NO_EDGE = "replicated_no_edge"
    #: Best-effort failure (origin missing, store/edge error); reader falls back.
    FAILED = "failed"


class PrefetchResult(BaseModel):
    """The outcome of warming one key into one region."""

    model_config = ConfigDict(frozen=True)

    key: str
    region_id: str
    outcome: PrefetchOutcome
    detail: str | None = None


class PrefetchPlan(BaseModel):
    """The result of a warm pass for one reader."""

    model_config = ConfigDict(frozen=True)

    region_id: str
    results: tuple[PrefetchResult, ...]

    @property
    def warmed(self) -> int:
        """How many keys are now warm (newly or already)."""
        return sum(
            1
            for r in self.results
            if r.outcome in (PrefetchOutcome.WARMED, PrefetchOutcome.ALREADY_WARM)
        )


class PrefetchController:
    """Warms upcoming shots into the reader's nearest region ahead of playback."""

    def __init__(
        self,
        *,
        manager: ReplicationManager,
        providers: Mapping[str, CdnProvider] | None = None,
        max_warm: int = DEFAULT_MAX_WARM,
    ) -> None:
        self._manager = manager
        self._topology = manager.topology
        self._providers = dict(providers or {})
        self._max_warm = max_warm

    def _nearest_region(
        self,
        hint: ReaderHint,
        health: Mapping[str, RegionHealth] | None,
    ) -> str:
        """The single nearest replica region for the reader (origin if alone).

        Prefers a replica; only origin warms when there are no replicas or every
        replica is unavailable.
        """
        health = health or {}
        replica_ids = self._topology.replica_ids()
        candidates = [
            rid
            for rid in replica_ids
            if (h := health.get(rid)) is None or h.available
        ]
        if not candidates:
            return self._topology.origin.region_id
        ranked = self._topology.rank(hint, health=health, candidates=candidates)
        return ranked[0][0]

    def upcoming_clip_keys(
        self,
        book_id: str,
        upcoming_shot_ids: Sequence[str],
    ) -> list[str]:
        """Map upcoming shot ids to their clip keys, capped at ``max_warm``."""
        from app.storage.object_store import keys as object_keys

        return [
            object_keys.clip(book_id, shot_id)
            for shot_id in upcoming_shot_ids[: self._max_warm]
        ]

    async def warm_keys(
        self,
        keys: Sequence[str],
        hint: ReaderHint,
        *,
        health: Mapping[str, RegionHealth] | None = None,
        ttl: int = 3600,
    ) -> PrefetchPlan:
        """Replicate + warm ``keys`` into the reader's nearest region.

        Best-effort and idempotent: each key is replicated to the chosen region
        (no-op if present) then warmed in the edge (skipped if already cached).
        Failures are captured per-key and never raised.
        """
        region_id = self._nearest_region(hint, health)
        provider = self._providers.get(region_id)
        results: list[PrefetchResult] = []
        for key in keys[: self._max_warm]:
            results.append(await self._warm_one(key, region_id, provider, ttl))
        plan = PrefetchPlan(region_id=region_id, results=tuple(results))
        logger.info(
            "cdn.prefetch",
            region_id=region_id,
            keys=len(results),
            warmed=plan.warmed,
        )
        return plan

    async def warm_upcoming(
        self,
        book_id: str,
        upcoming_shot_ids: Sequence[str],
        hint: ReaderHint,
        *,
        health: Mapping[str, RegionHealth] | None = None,
        ttl: int = 3600,
    ) -> PrefetchPlan:
        """Convenience: warm the upcoming *shots* of a book for a reader."""
        return await self.warm_keys(
            self.upcoming_clip_keys(book_id, upcoming_shot_ids),
            hint,
            health=health,
            ttl=ttl,
        )

    async def _warm_one(
        self,
        key: str,
        region_id: str,
        provider: CdnProvider | None,
        ttl: int,
    ) -> PrefetchResult:
        """Replicate one key to the region then warm its edge (best-effort)."""
        try:
            if region_id != self._topology.origin.region_id:
                report = await self._manager.replicate(key, targets=[region_id])
                if not report.fully_replicated:
                    fail = report.failures()[0]
                    return PrefetchResult(
                        key=key,
                        region_id=region_id,
                        outcome=PrefetchOutcome.FAILED,
                        detail=fail.detail or "replication failed",
                    )
            if provider is None:
                return PrefetchResult(
                    key=key,
                    region_id=region_id,
                    outcome=PrefetchOutcome.REPLICATED_NO_EDGE,
                )
            if await provider.is_cached(key):
                return PrefetchResult(
                    key=key,
                    region_id=region_id,
                    outcome=PrefetchOutcome.ALREADY_WARM,
                )
            store = self._manager.store_for(region_id)
            origin_url = sign_url(store, key, now=self._manager.clock.now(), ttl=ttl).url
            await provider.warm(key, origin_url)
            return PrefetchResult(
                key=key,
                region_id=region_id,
                outcome=PrefetchOutcome.WARMED,
            )
        except Exception as exc:  # noqa: BLE001 - prefetch is strictly best-effort
            logger.warning(
                "cdn.prefetch.failed", key=key, region_id=region_id, error=str(exc)
            )
            return PrefetchResult(
                key=key,
                region_id=region_id,
                outcome=PrefetchOutcome.FAILED,
                detail=str(exc),
            )


__all__ = [
    "DEFAULT_MAX_WARM",
    "PrefetchController",
    "PrefetchOutcome",
    "PrefetchPlan",
    "PrefetchResult",
]
