"""Cold-start cost model + measured first-vs-warm latency tracking (pure).

Kinora generates film a few seconds *ahead* of the reader, so the latency of the
**first** request to a provider — the auth handshake, connection establishment,
signed-session minting — is the enemy. This module measures that cost per
provider and exposes the gap between a *cold* open and a *warm* reuse so the
pre-warm scheduler can keep exactly enough sessions warm to hide it.

Everything here is pure arithmetic over samples handed in by the pool (which
times every ``factory.open`` and every borrow). No clock, no I/O — the pool owns
"now"; this owns the statistics. State is small and JSON-serialisable so it can
round-trip through the same Redis path the scheduler model uses.

The estimator is an EWMA so it tracks drift (a provider that warms up over the
session, a region that slows under load) without chasing a single outlier, and it
keeps a conservative high-watermark so the warm target is sized against a *bad*
cold start, not the average one.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: EWMA smoothing for latency means: weights the last ~6 samples meaningfully.
DEFAULT_LATENCY_ALPHA = 0.3
#: How much of the observed cold-start max to trust as the planning figure. The
#: warm target is sized against ``p_high = mean + (max - mean) * this`` so a
#: single slow handshake nudges the target up without overreacting to it.
DEFAULT_COLD_HEADROOM = 0.5
#: Seed cold-start estimate (seconds) before any sample — a deliberately non-trivial
#: figure so a freshly-seen provider is treated as having real cold-start cost.
DEFAULT_SEED_COLD_S = 2.0
#: Seed warm-reuse estimate (seconds) — borrowing a warm session is near-free.
DEFAULT_SEED_WARM_S = 0.05


class LatencyStats(BaseModel):
    """EWMA mean + observed max + sample count for one latency series."""

    mean_s: float = 0.0
    max_s: float = 0.0
    samples: int = 0

    def observe(self, value_s: float, *, alpha: float = DEFAULT_LATENCY_ALPHA) -> LatencyStats:
        """Return a new stats with ``value_s`` folded in (immutable update)."""
        v = max(0.0, float(value_s))
        if self.samples == 0:
            return LatencyStats(mean_s=v, max_s=v, samples=1)
        mean = (1.0 - alpha) * self.mean_s + alpha * v
        return LatencyStats(mean_s=mean, max_s=max(self.max_s, v), samples=self.samples + 1)


class ColdStartModel(BaseModel):
    """Per-provider cold-vs-warm latency model feeding the warm target.

    ``cold`` tracks the latency of opening a *fresh* session (the cost we hide);
    ``warm`` tracks the latency of borrowing an *already-warm* one (the floor).
    The *savings* (cold − warm) is what a warm session buys, and ``planning_cold_s``
    is the conservative figure the demand model sizes the warm target against.
    """

    provider: str
    cold: LatencyStats = Field(default_factory=lambda: LatencyStats(mean_s=DEFAULT_SEED_COLD_S))
    warm: LatencyStats = Field(default_factory=lambda: LatencyStats(mean_s=DEFAULT_SEED_WARM_S))
    alpha: float = DEFAULT_LATENCY_ALPHA
    cold_headroom: float = DEFAULT_COLD_HEADROOM

    def record_cold_open(self, latency_s: float) -> None:
        """Fold in one measured ``factory.open`` (cold-start) latency."""
        self.cold = self.cold.observe(latency_s, alpha=self.alpha)

    def record_warm_borrow(self, latency_s: float) -> None:
        """Fold in one measured warm-session borrow latency (the floor)."""
        self.warm = self.warm.observe(latency_s, alpha=self.alpha)

    @property
    def planning_cold_s(self) -> float:
        """Conservative cold-start figure: mean nudged toward the observed max.

        ``mean + (max − mean) * cold_headroom`` — sized against a *bad* cold start
        so the warm target hides tail latency, not just the average.
        """
        c = self.cold
        if c.samples == 0:
            return DEFAULT_SEED_COLD_S
        return c.mean_s + max(0.0, c.max_s - c.mean_s) * self.cold_headroom

    @property
    def warm_s(self) -> float:
        """Expected warm-borrow latency (the floor a warm session achieves)."""
        return self.warm.mean_s if self.warm.samples else DEFAULT_SEED_WARM_S

    @property
    def savings_s(self) -> float:
        """Latency a warm session saves over a cold open (never negative)."""
        return max(0.0, self.planning_cold_s - self.warm_s)

    def worth_warming(self, *, threshold_s: float) -> bool:
        """Is the cold-start cost big enough that pre-warming pays for itself?

        A provider whose cold open is already as fast as a warm borrow (savings
        below ``threshold_s``) is not worth holding idle sessions for — the demand
        model uses this to drop the warm floor to zero for cheap-to-open providers.
        """
        return self.savings_s >= max(0.0, threshold_s)


__all__ = [
    "DEFAULT_COLD_HEADROOM",
    "DEFAULT_LATENCY_ALPHA",
    "DEFAULT_SEED_COLD_S",
    "DEFAULT_SEED_WARM_S",
    "ColdStartModel",
    "LatencyStats",
]
