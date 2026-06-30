"""Multi-region asset CDN / replication layer (``app.cdn``).

A read-and-distribution layer layered **over** the existing
:class:`app.storage.object_store.ObjectStore` interface (never replacing it).
Generated clips/audio/keyframes land in the **origin** bucket; this package
mirrors them to regional replicas, hands each reader the *nearest healthy*
copy, signs expiring range-friendly URLs over provider differences, models the
edge-cache policy (immutable content-addressed vs purge-on-invalidate mutable),
and warms the next-likely shots into the reader's region ahead of playback — so
playback is fast and resilient even when the reader is far from origin.

Pure async logic over injectable seams (:mod:`app.cdn.protocols`); the real
boto3 store is bound in via :mod:`app.cdn.adapters`, and deterministic fakes for
tests live in :mod:`app.cdn.testing`.

Public surface (import-light — :mod:`app.cdn.adapters` is imported separately so
``import app.cdn`` never pulls in boto3):

* :mod:`app.cdn.errors` — typed errors under :class:`CdnError`.
* :mod:`app.cdn.regions` — region topology + geo/latency nearest-region scoring.
* :mod:`app.cdn.protocols` — injectable store / CDN-provider / clock seams.
* :mod:`app.cdn.replication` — idempotent, checksum-verified mirror + reconcile.
* :mod:`app.cdn.resolver` — nearest-healthy-replica resolution with origin failover.
* :mod:`app.cdn.signing` — provider-abstracted expiring, range-friendly URLs.
* :mod:`app.cdn.cache` — edge-cache policy (TTL / immutable / purge).
* :mod:`app.cdn.prefetch` — warm-the-next-shots prefetch controller.
"""

from __future__ import annotations

from app.cdn.cache import (
    CacheClass,
    CachePolicy,
    EdgeCachePolicy,
    classify_key,
)
from app.cdn.errors import (
    CachePurgeError,
    CdnError,
    NoHealthyReplicaError,
    NoOriginError,
    OriginMissingObjectError,
    ReplicaChecksumMismatchError,
    UnknownRegionError,
)
from app.cdn.prefetch import (
    PrefetchController,
    PrefetchOutcome,
    PrefetchPlan,
    PrefetchResult,
)
from app.cdn.protocols import CdnProvider, Clock, RegionStore
from app.cdn.regions import (
    GeoPoint,
    ReaderHint,
    Region,
    RegionHealth,
    RegionTopology,
)
from app.cdn.replication import (
    ReplicaResult,
    ReplicaStatus,
    ReplicationLedger,
    ReplicationManager,
    ReplicationReport,
    ReplicationState,
)
from app.cdn.resolver import AssetResolver, Resolution
from app.cdn.signing import SignedUrl, sign_url

__all__ = [
    "AssetResolver",
    "CacheClass",
    "CachePolicy",
    "CachePurgeError",
    "CdnError",
    "CdnProvider",
    "Clock",
    "EdgeCachePolicy",
    "GeoPoint",
    "NoHealthyReplicaError",
    "NoOriginError",
    "OriginMissingObjectError",
    "PrefetchController",
    "PrefetchOutcome",
    "PrefetchPlan",
    "PrefetchResult",
    "ReaderHint",
    "Region",
    "RegionHealth",
    "RegionStore",
    "RegionTopology",
    "ReplicaChecksumMismatchError",
    "ReplicaResult",
    "ReplicaStatus",
    "ReplicationLedger",
    "ReplicationManager",
    "ReplicationReport",
    "ReplicationState",
    "Resolution",
    "SignedUrl",
    "UnknownRegionError",
    "classify_key",
    "sign_url",
]
