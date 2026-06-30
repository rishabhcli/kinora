"""Region-aware asset resolver: hand a reader the nearest healthy replica.

Given a reader hint (geo / latency / explicit region), resolve a key to a
:class:`app.cdn.signing.SignedUrl` pointing at the *nearest replica that
actually holds a fresh, healthy copy* — failing over to origin when no replica
qualifies. This is the read path that makes playback fast for a reader far from
the origin bucket.

A replica qualifies only if it:

1. is in the topology and has the object recorded in the replication ledger,
2. is marked ``available`` by the injected health view, and
3. is within the configured ``max_lag_s`` of origin (a too-stale replica is
   skipped so a reader never gets bytes that predate a Director re-render).

Candidates are ranked nearest-first by :meth:`RegionTopology.rank` (measured
RTT > geo distance > continent affinity), then the first qualifying one wins.
If none qualifies, the resolver falls back to **origin** — which always holds
the canonical bytes — unless origin itself is unhealthy, in which case it raises
:class:`NoHealthyReplicaError` (every served region is down).

Pure async logic over the manager + injected health/clock; no network in tests.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from app.cdn.errors import NoHealthyReplicaError, OriginMissingObjectError
from app.cdn.protocols import Clock
from app.cdn.regions import ReaderHint, RegionHealth
from app.cdn.replication import ReplicationManager
from app.cdn.signing import DEFAULT_TTL_S, EdgeTokenSigner, SignedUrl, sign_url
from app.core.logging import get_logger

logger = get_logger("app.cdn.resolver")

#: A replica more than this many seconds behind origin is treated as stale and
#: skipped in favour of a fresher region (or origin).
DEFAULT_MAX_LAG_S = 60.0


class Resolution(BaseModel):
    """The outcome of resolving a key for a reader."""

    model_config = ConfigDict(frozen=True)

    key: str
    #: The region chosen to serve the object.
    region_id: str
    #: Whether the resolver fell back to origin (no replica qualified).
    served_from_origin: bool
    #: The browser-reachable signed URL.
    signed_url: SignedUrl
    #: Modelled reader→region RTT (ms) for the chosen region (observability).
    rtt_ms: float
    #: Replica regions that were considered but skipped, with the reason.
    skipped: tuple[tuple[str, str], ...] = ()


class AssetResolver:
    """Resolves a key to the nearest healthy replica, failing over to origin."""

    def __init__(
        self,
        *,
        manager: ReplicationManager,
        clock: Clock,
        max_lag_s: float = DEFAULT_MAX_LAG_S,
        edges: Mapping[str, EdgeTokenSigner] | None = None,
    ) -> None:
        self._manager = manager
        self._topology = manager.topology  # shared, immutable topology
        self._clock = clock
        self._max_lag_s = max_lag_s
        self._edges = dict(edges or {})

    async def resolve(
        self,
        key: str,
        hint: ReaderHint,
        *,
        health: Mapping[str, RegionHealth] | None = None,
        ttl: int = DEFAULT_TTL_S,
    ) -> Resolution:
        """Resolve ``key`` to the nearest healthy replica (origin fallback).

        ``health`` carries per-region availability / measured-RTT / lag overrides
        from the edge; absent, every region is assumed available with the static
        geo cost model and the ledger's recorded lag.
        """
        health = health or {}
        origin_id = self._topology.origin.region_id

        # Rank every replica region nearest-first; origin is the explicit fallback.
        replica_ids = self._topology.replica_ids()
        ranked = self._topology.rank(hint, health=health, candidates=replica_ids)

        skipped: list[tuple[str, str]] = []
        for region_id, rtt_ms in ranked:
            reason = await self._disqualify(region_id, key, health.get(region_id))
            if reason is not None:
                skipped.append((region_id, reason))
                continue
            signed = self._sign(region_id, key, ttl)
            logger.info(
                "cdn.resolve.replica",
                key=key,
                region_id=region_id,
                rtt_ms=round(rtt_ms, 2),
                skipped=len(skipped),
            )
            return Resolution(
                key=key,
                region_id=region_id,
                served_from_origin=False,
                signed_url=signed,
                rtt_ms=rtt_ms,
                skipped=tuple(skipped),
            )

        # Failover to origin — it always holds the canonical bytes.
        origin_health = health.get(origin_id)
        if origin_health is not None and not origin_health.available:
            logger.error("cdn.resolve.no_healthy_region", key=key)
            raise NoHealthyReplicaError(key)
        if not await self._manager.origin_store.exists(key):
            raise OriginMissingObjectError(key, origin_id)
        origin_rtt = self._topology.modelled_rtt_ms(
            self._topology.origin, hint, origin_health
        )
        signed = self._sign(origin_id, key, ttl)
        logger.info(
            "cdn.resolve.origin_fallback",
            key=key,
            region_id=origin_id,
            skipped=len(skipped),
        )
        return Resolution(
            key=key,
            region_id=origin_id,
            served_from_origin=True,
            signed_url=signed,
            rtt_ms=origin_rtt,
            skipped=tuple(skipped),
        )

    async def _disqualify(
        self,
        region_id: str,
        key: str,
        health: RegionHealth | None,
    ) -> str | None:
        """Return a skip-reason if this replica can't serve ``key``, else ``None``."""
        if health is not None and not health.available:
            return "unavailable"
        state = self._manager.ledger.get(region_id, key)
        if state is None:
            return "missing"
        # Prefer a health-supplied lag (live) over the ledger's recorded lag.
        lag = health.replication_lag_s if health is not None else state.lag_s()
        if lag > self._max_lag_s:
            return f"stale(lag={lag:.1f}s)"
        return None

    def _sign(self, region_id: str, key: str, ttl: int) -> SignedUrl:
        """Mint a signed URL for ``key`` in ``region_id`` (edge token if present)."""
        store = self._manager.store_for(region_id)
        return sign_url(
            store,
            key,
            now=self._clock.now(),
            ttl=ttl,
            edge=self._edges.get(region_id),
        )


__all__ = ["DEFAULT_MAX_LAG_S", "AssetResolver", "Resolution"]
