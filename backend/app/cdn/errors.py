"""Typed errors for the multi-region CDN / replication subsystem.

A small, flat hierarchy under :class:`CdnError` so callers can catch the whole
subsystem (``except CdnError``) or a specific failure. Kept dependency-free so
every other module can import it without cycles.
"""

from __future__ import annotations


class CdnError(RuntimeError):
    """Base class for every error raised by :mod:`app.cdn`."""


class UnknownRegionError(CdnError):
    """A region id was referenced that is not part of the configured topology."""

    def __init__(self, region_id: str) -> None:
        self.region_id = region_id
        super().__init__(f"unknown region {region_id!r}")


class NoOriginError(CdnError):
    """No origin region is configured (replication has nowhere to copy from)."""


class OriginMissingObjectError(CdnError):
    """The object is absent from the origin, so it cannot be replicated/served.

    Distinct from a *replica* miss (which simply triggers failover to origin):
    if origin itself lacks the bytes there is nothing to serve anywhere.
    """

    def __init__(self, key: str, origin_region: str) -> None:
        self.key = key
        self.origin_region = origin_region
        super().__init__(f"object {key!r} missing from origin region {origin_region!r}")


class ReplicaChecksumMismatchError(CdnError):
    """A replicated object's checksum diverged from the origin's recorded digest.

    Raised by checksum-verified replication and by the reconcile/repair sweep
    when a replica's bytes have drifted (bit-rot, partial write, tampering).
    """

    def __init__(self, key: str, region_id: str, expected: str, actual: str) -> None:
        self.key = key
        self.region_id = region_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"replica checksum mismatch for {key!r} in region {region_id!r}: "
            f"expected {expected[:12]}…, got {actual[:12]}…"
        )


class NoHealthyReplicaError(CdnError):
    """No healthy replica *or* origin can currently serve the requested object.

    Resolution always falls back to origin; this is only raised when origin is
    itself unavailable (down/unhealthy) and no replica holds a fresh copy.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"no healthy replica or origin can serve {key!r}")


class CachePurgeError(CdnError):
    """An edge-cache purge against the pluggable CDN provider failed."""

    def __init__(self, key: str, reason: str) -> None:
        self.key = key
        self.reason = reason
        super().__init__(f"failed to purge {key!r} from edge cache: {reason}")


__all__ = [
    "CachePurgeError",
    "CdnError",
    "NoHealthyReplicaError",
    "NoOriginError",
    "OriginMissingObjectError",
    "ReplicaChecksumMismatchError",
    "UnknownRegionError",
]
