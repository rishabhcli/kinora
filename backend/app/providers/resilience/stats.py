"""Inspection / telemetry snapshots for the resilience gateway.

A single JSON-friendly view that bundles the gateway's sub-component stats (rate
limiter, breakers, cache, hedging, metering) so a ``/api/providers/gateway``
debug route — or a test — can read the whole resilience posture in one shot
without reaching into each component. Pure data; no behavior.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .breakers import BreakerSnapshot
from .cache import CacheStats
from .hedging import HedgeStats
from .metering import MeterSnapshot


@dataclass(frozen=True, slots=True)
class GatewayCallStats:
    """Aggregate call outcomes the gateway tallies itself."""

    calls: int = 0
    successes: int = 0
    failures: int = 0
    retries: int = 0
    breaker_rejections: int = 0
    throttles_observed: int = 0
    cache_hits: int = 0


@dataclass
class GatewaySnapshot:
    """The full resilience posture of one gateway, JSON-serializable."""

    rate: float
    in_cooldown: bool
    calls: GatewayCallStats
    breakers: list[BreakerSnapshot] = field(default_factory=list)
    cache: CacheStats | None = None
    hedging: HedgeStats | None = None
    metering: MeterSnapshot | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "rate": round(self.rate, 4),
            "in_cooldown": self.in_cooldown,
            "calls": asdict(self.calls),
            "breakers": [
                {
                    "model": b.model,
                    "state": b.state.value,
                    "consecutive_failures": b.consecutive_failures,
                    "total_successes": b.total_successes,
                    "total_failures": b.total_failures,
                    "total_rejections": b.total_rejections,
                }
                for b in self.breakers
            ],
        }
        if self.cache is not None:
            out["cache"] = {
                "hits": self.cache.hits,
                "misses": self.cache.misses,
                "coalesced": self.cache.coalesced,
                "evictions": self.cache.evictions,
                "expirations": self.cache.expirations,
                "hit_rate": round(self.cache.hit_rate, 4),
            }
        if self.hedging is not None:
            out["hedging"] = asdict(self.hedging)
        if self.metering is not None:
            out["metering"] = {
                "total": self.metering.total,
                "by_model": self.metering.by_model,
                "by_operation": self.metering.by_operation,
                "fanout_errors": self.metering.fanout_errors,
            }
        return out


__all__ = [
    "GatewayCallStats",
    "GatewaySnapshot",
]
