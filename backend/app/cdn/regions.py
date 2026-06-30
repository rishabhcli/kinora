"""Region topology + geo/latency-hint nearest-region scoring.

A reader far from the origin bucket should be served from the *nearest healthy
replica*, not the origin. "Nearest" is resolved from a cheap, deterministic hint
the reader (or an upstream geo-IP edge) supplies:

* an explicit ``region_id`` (the strongest hint — the edge already knows),
* a geo coordinate (lat/lon) scored by great-circle distance, or
* a coarse ``continent`` / ``country`` bucket scored by a static affinity table.

Everything here is pure and offline: no network, no live latency probes. The
real latency signal (RUM/synthetic) feeds in later as a per-region ``rtt_ms``
override carried on :class:`RegionHealth`; the scorer simply prefers the lower
modelled cost. Determinism (stable tie-breaks by region id) keeps tests exact.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.cdn.errors import NoOriginError, UnknownRegionError

#: A modelled RTT (ms) assigned when a reader hint cannot be located at all —
#: large enough to lose to any geographically-scored region but finite so the
#: scorer still produces a total order.
UNKNOWN_HINT_RTT_MS = 10_000.0

#: Earth mean radius (km) for the haversine great-circle distance.
_EARTH_RADIUS_KM = 6371.0

#: Rough propagation: fibre carries light at ~⅔ c, plus router/queueing
#: overhead, so ~1 ms of RTT per ~100 km is a serviceable offline model.
_KM_PER_RTT_MS = 100.0


class GeoPoint(BaseModel):
    """A latitude/longitude pair (decimal degrees)."""

    model_config = ConfigDict(frozen=True)

    lat: Annotated[float, Field(ge=-90.0, le=90.0)]
    lon: Annotated[float, Field(ge=-180.0, le=180.0)]


class Region(BaseModel):
    """One bucket location in the replication topology.

    A region maps 1:1 to an injected object store (see
    :class:`app.cdn.protocols.RegionStore`). Exactly one region is the
    ``origin`` — the bucket the render pipeline writes to first.
    """

    model_config = ConfigDict(frozen=True)

    region_id: str
    #: Human label, e.g. "US East (N. Virginia)".
    name: str = ""
    #: Geographic anchor used for great-circle scoring of geo hints.
    location: GeoPoint | None = None
    #: Coarse continent bucket (e.g. "na", "eu", "ap") for country/continent hints.
    continent: str = ""
    #: Whether this region is the write origin. Exactly one must be the origin.
    origin: bool = False


class RegionHealth(BaseModel):
    """Live(ish) health/quality signal for a region, injected at resolve time.

    Carries the optional measured/synthetic ``rtt_ms`` that overrides the static
    geo model, plus availability and the replication-lag view the resolver uses
    to skip a replica that is too far behind origin.
    """

    model_config = ConfigDict(frozen=True)

    region_id: str
    #: Whether the region's store is currently reachable / serving.
    available: bool = True
    #: Measured RTT from the reader to this region (ms), if known. Overrides the
    #: static great-circle estimate when present.
    rtt_ms: float | None = None
    #: Seconds the region's replica view lags origin (0 == fully caught up).
    replication_lag_s: float = 0.0


class ReaderHint(BaseModel):
    """What the edge knows about where a reader is, for nearest-region routing.

    All fields optional and independently usable; the scorer applies them in
    decreasing order of trust: explicit region → geo point → continent/country.
    """

    model_config = ConfigDict(frozen=True)

    #: The reader is already pinned to a region by the edge (strongest hint).
    region_id: str | None = None
    #: Decimal-degree coordinate (e.g. from a geo-IP lookup).
    geo: GeoPoint | None = None
    #: Coarse continent code matching :attr:`Region.continent`.
    continent: str | None = None
    #: ISO country code; mapped to a continent via :data:`COUNTRY_CONTINENT`.
    country: str | None = None


#: Minimal ISO-3166 → continent bucket map (extend as the topology grows). Only
#: the codes the demo topology needs; an unmapped country degrades to the geo
#: or region hint rather than erroring.
COUNTRY_CONTINENT: Mapping[str, str] = {
    "US": "na",
    "CA": "na",
    "MX": "na",
    "GB": "eu",
    "DE": "eu",
    "FR": "eu",
    "NL": "eu",
    "IE": "eu",
    "IN": "ap",
    "SG": "ap",
    "JP": "ap",
    "AU": "ap",
    "CN": "ap",
    "BR": "sa",
}


def haversine_km(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance between two points in kilometres."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def _resolved_continent(hint: ReaderHint) -> str | None:
    """Best continent guess from a hint (explicit continent wins over country)."""
    if hint.continent:
        return hint.continent
    if hint.country:
        return COUNTRY_CONTINENT.get(hint.country.upper())
    return None


class RegionTopology:
    """An immutable set of regions with origin lookup and nearest-region scoring.

    Pure logic: the topology owns no store and does no I/O. The replication
    manager / resolver pair the topology with injected stores keyed by
    ``region_id``.
    """

    def __init__(self, regions: Iterable[Region]) -> None:
        self._by_id: dict[str, Region] = {}
        origin: Region | None = None
        for region in regions:
            if region.region_id in self._by_id:
                raise ValueError(f"duplicate region id {region.region_id!r}")
            self._by_id[region.region_id] = region
            if region.origin:
                if origin is not None:
                    raise ValueError(
                        "topology has multiple origin regions: "
                        f"{origin.region_id!r} and {region.region_id!r}"
                    )
                origin = region
        if not self._by_id:
            raise ValueError("topology must contain at least one region")
        if origin is None:
            raise NoOriginError("topology has no origin region")
        self._origin = origin

    @property
    def origin(self) -> Region:
        """The single write-origin region."""
        return self._origin

    @property
    def region_ids(self) -> tuple[str, ...]:
        """All region ids in stable insertion order."""
        return tuple(self._by_id)

    def regions(self) -> tuple[Region, ...]:
        """All regions in stable insertion order."""
        return tuple(self._by_id.values())

    def replica_ids(self) -> tuple[str, ...]:
        """Region ids that are *not* the origin (replication targets)."""
        return tuple(rid for rid in self._by_id if rid != self._origin.region_id)

    def get(self, region_id: str) -> Region:
        """Return the region with ``region_id`` or raise :class:`UnknownRegionError`."""
        try:
            return self._by_id[region_id]
        except KeyError:
            raise UnknownRegionError(region_id) from None

    def __contains__(self, region_id: object) -> bool:
        return region_id in self._by_id

    def modelled_rtt_ms(
        self,
        region: Region,
        hint: ReaderHint,
        health: RegionHealth | None = None,
    ) -> float:
        """Estimate the reader→region RTT (ms) under the offline cost model.

        Trust order: a measured ``rtt_ms`` on ``health`` wins outright; else an
        explicit ``region_id`` hint gives the region's own location a zero-cost
        bonus; else a geo point is scored by great-circle distance; else a
        continent match gives a flat in-continent cost; else
        :data:`UNKNOWN_HINT_RTT_MS`.
        """
        if health is not None and health.rtt_ms is not None:
            return float(health.rtt_ms)
        if hint.region_id is not None and hint.region_id == region.region_id:
            return 0.0
        if hint.geo is not None and region.location is not None:
            return haversine_km(hint.geo, region.location) / _KM_PER_RTT_MS
        continent = _resolved_continent(hint)
        if continent is not None and region.continent:
            # In-continent regions are cheap; cross-continent are penalised so a
            # same-continent replica beats a far one even without coordinates.
            return 20.0 if continent == region.continent else 150.0
        return UNKNOWN_HINT_RTT_MS

    def rank(
        self,
        hint: ReaderHint,
        *,
        health: Mapping[str, RegionHealth] | None = None,
        candidates: Iterable[str] | None = None,
    ) -> list[tuple[str, float]]:
        """Rank candidate regions nearest-first as ``(region_id, rtt_ms)``.

        Ties break on region id (lexicographic) so the order is deterministic.
        ``candidates`` defaults to the whole topology; unknown candidate ids
        raise :class:`UnknownRegionError`.
        """
        health = health or {}
        ids = tuple(candidates) if candidates is not None else self.region_ids
        scored: list[tuple[float, str]] = []
        for rid in ids:
            region = self.get(rid)
            cost = self.modelled_rtt_ms(region, hint, health.get(rid))
            scored.append((cost, rid))
        scored.sort(key=lambda pair: (pair[0], pair[1]))
        return [(rid, cost) for cost, rid in scored]


__all__ = [
    "COUNTRY_CONTINENT",
    "UNKNOWN_HINT_RTT_MS",
    "GeoPoint",
    "ReaderHint",
    "Region",
    "RegionHealth",
    "RegionTopology",
    "haversine_km",
]
