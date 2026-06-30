"""Per-provider quota accounting over fixed (tumbling) time windows.

Each video provider ships its own limits — DashScope-intl throttles by
requests-per-minute and a free-tier video-seconds pool; MiniMax bills per clip
against a USD cap; a self-hosted lane is bounded by concurrent GPU slots. The
:class:`QuotaLimits` captures all four dimensions a render submission can exhaust:

* **requests/min** — a tumbling 60s window of submission count.
* **concurrent jobs** — an in-flight gauge (incremented on submit, released on
  terminal completion).
* **daily video-seconds** — the scarce, hard-capped resource (§11.1), in a
  tumbling 24h calendar window.
* **monthly spend (USD)** — a belt-and-suspenders cost cap in a tumbling 30d
  window.

:class:`QuotaAccountant` evaluates an admission against every dimension through the
shared :class:`~app.video.governor.store.GovernorStore`, so the decision is correct
across all processes that submit to the same provider. ``check`` is a *dry run*
(no mutation); ``reserve`` admits and records; ``release`` returns a held
concurrency slot. Window math is pure and clock-driven — a fake clock exercises
every rollover deterministically.

A dimension with limit ``0`` or ``None`` means *unbounded* on that axis (e.g. a
single-tenant local run with no monthly cap), so the governor degrades gracefully
when a provider publishes no explicit limit.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from .clock import Clock, monotonic
from .store import GovernorStore

#: Tumbling window lengths in seconds.
_MINUTE_S = 60
_DAY_S = 86_400
_MONTH_S = 30 * 86_400


class QuotaDimension(StrEnum):
    """The axes a video submission can exhaust on a provider."""

    REQUESTS_PER_MIN = "requests_per_min"
    CONCURRENT_JOBS = "concurrent_jobs"
    DAILY_VIDEO_SECONDS = "daily_video_seconds"
    MONTHLY_SPEND_USD = "monthly_spend_usd"


@dataclass(frozen=True, slots=True)
class QuotaLimits:
    """A provider's published ceilings (``0``/``None`` ⇒ unbounded on that axis)."""

    requests_per_min: int | None = None
    concurrent_jobs: int | None = None
    daily_video_seconds: float | None = None
    monthly_spend_usd: float | None = None
    #: Fractions of any limit at which to fire a ``QUOTA_NEAR_LIMIT`` alert. The
    #: governor reads these; the accountant only reports utilisation.
    alert_fractions: tuple[float, ...] = (0.75, 0.90)

    def limit_for(self, dimension: QuotaDimension) -> float | None:
        match dimension:
            case QuotaDimension.REQUESTS_PER_MIN:
                return None if not self.requests_per_min else float(self.requests_per_min)
            case QuotaDimension.CONCURRENT_JOBS:
                return None if not self.concurrent_jobs else float(self.concurrent_jobs)
            case QuotaDimension.DAILY_VIDEO_SECONDS:
                return self.daily_video_seconds or None
            case QuotaDimension.MONTHLY_SPEND_USD:
                return self.monthly_spend_usd or None
        return None


@dataclass(frozen=True, slots=True)
class RenderCost:
    """What admitting one render would consume on each quota axis."""

    video_seconds: float = 0.0
    spend_usd: float = 0.0
    #: A submission always costs one request and one concurrency slot unless the
    #: caller is pre-reserving capacity without an actual submit.
    requests: int = 1
    concurrent: int = 1


@dataclass(frozen=True, slots=True)
class DimensionUsage:
    """The observed usage / limit / utilisation of one quota dimension."""

    dimension: QuotaDimension
    used: float
    limit: float | None
    incoming: float

    @property
    def unbounded(self) -> bool:
        return self.limit is None

    @property
    def projected(self) -> float:
        """Usage *after* admitting the incoming cost."""
        return self.used + self.incoming

    @property
    def utilisation(self) -> float:
        """Fraction of the limit already used (0.0 when unbounded)."""
        if self.limit is None or self.limit <= 0:
            return 0.0
        return self.used / self.limit

    @property
    def would_exceed(self) -> bool:
        """True when admitting the incoming cost would breach the limit."""
        if self.limit is None:
            return False
        return self.projected > self.limit + 1e-9


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    """The outcome of a quota check across every dimension."""

    provider: str
    admitted: bool
    usages: tuple[DimensionUsage, ...]

    @property
    def blocking(self) -> tuple[DimensionUsage, ...]:
        """The dimensions (if any) that would be exceeded."""
        return tuple(u for u in self.usages if u.would_exceed)

    def usage(self, dimension: QuotaDimension) -> DimensionUsage | None:
        for u in self.usages:
            if u.dimension is dimension:
                return u
        return None

    @property
    def max_utilisation(self) -> float:
        """The highest utilisation across bounded dimensions (0.0 if none)."""
        bounded = [u.utilisation for u in self.usages if not u.unbounded]
        return max(bounded) if bounded else 0.0


def window_start(now: float, length_s: int) -> int:
    """The tumbling-window start (epoch second) containing ``now`` for ``length_s``."""
    return int(now) - (int(now) % length_s)


class QuotaAccountant:
    """Evaluate and record a provider's multi-axis quota through the store.

    The accountant is stateless beyond its configuration; all counters live in the
    injected :class:`GovernorStore`, so two accountants for the same provider name
    (in two processes) account against the same windows.
    """

    def __init__(
        self,
        provider: str,
        limits: QuotaLimits,
        store: GovernorStore,
        *,
        clock: Clock = monotonic,
    ) -> None:
        self.provider = provider
        self.limits = limits
        self._store = store
        self._clock = clock

    # -- keys ------------------------------------------------------------- #

    def _win_key(self, dimension: QuotaDimension) -> str:
        return f"gov:quota:{self.provider}:{dimension.value}"

    def _gauge_key(self) -> str:
        return f"gov:concurrent:{self.provider}"

    # -- read-only evaluation --------------------------------------------- #

    async def _windowed_usages(self, cost: RenderCost) -> list[DimensionUsage]:
        now = self._clock()
        rpm_win = window_start(now, _MINUTE_S)
        day_win = window_start(now, _DAY_S)
        month_win = window_start(now, _MONTH_S)

        rpm_used = await self._store.read_window(
            self._win_key(QuotaDimension.REQUESTS_PER_MIN), rpm_win
        )
        day_used = await self._store.read_window(
            self._win_key(QuotaDimension.DAILY_VIDEO_SECONDS), day_win
        )
        month_used = await self._store.read_window(
            self._win_key(QuotaDimension.MONTHLY_SPEND_USD), month_win
        )
        concurrent = await self._store.read_gauge(self._gauge_key())

        return [
            DimensionUsage(
                QuotaDimension.REQUESTS_PER_MIN,
                used=rpm_used,
                limit=self.limits.limit_for(QuotaDimension.REQUESTS_PER_MIN),
                incoming=float(cost.requests),
            ),
            DimensionUsage(
                QuotaDimension.CONCURRENT_JOBS,
                used=float(concurrent),
                limit=self.limits.limit_for(QuotaDimension.CONCURRENT_JOBS),
                incoming=float(cost.concurrent),
            ),
            DimensionUsage(
                QuotaDimension.DAILY_VIDEO_SECONDS,
                used=day_used,
                limit=self.limits.limit_for(QuotaDimension.DAILY_VIDEO_SECONDS),
                incoming=cost.video_seconds,
            ),
            DimensionUsage(
                QuotaDimension.MONTHLY_SPEND_USD,
                used=month_used,
                limit=self.limits.limit_for(QuotaDimension.MONTHLY_SPEND_USD),
                incoming=cost.spend_usd,
            ),
        ]

    async def check(self, cost: RenderCost) -> QuotaDecision:
        """A dry-run admission decision for ``cost`` (mutates nothing)."""
        usages = await self._windowed_usages(cost)
        admitted = not any(u.would_exceed for u in usages)
        return QuotaDecision(self.provider, admitted=admitted, usages=tuple(usages))

    async def utilisation(self) -> QuotaDecision:
        """Current utilisation with a zero incoming cost (for the oracle/SLA panel)."""
        return await self.check(RenderCost(video_seconds=0.0, spend_usd=0.0,
                                           requests=0, concurrent=0))

    # -- mutating reserve / release --------------------------------------- #

    async def reserve(self, cost: RenderCost) -> QuotaDecision:
        """Admit ``cost`` iff it fits every dimension, recording counters atomically.

        Returns the decision. On admission the request count, concurrency gauge,
        video-seconds, and spend are all advanced; on refusal nothing is recorded.
        The concurrency gauge is the only axis released later (via :meth:`release`)
        — windowed counters tumble on their own.
        """
        decision = await self.check(cost)
        if not decision.admitted:
            return decision

        now = self._clock()
        if cost.requests:
            await self._store.incr_window(
                self._win_key(QuotaDimension.REQUESTS_PER_MIN),
                window_start(now, _MINUTE_S),
                float(cost.requests),
                ttl_s=_MINUTE_S * 2,
            )
        if cost.video_seconds:
            await self._store.incr_window(
                self._win_key(QuotaDimension.DAILY_VIDEO_SECONDS),
                window_start(now, _DAY_S),
                cost.video_seconds,
                ttl_s=_DAY_S * 2,
            )
        if cost.spend_usd:
            await self._store.incr_window(
                self._win_key(QuotaDimension.MONTHLY_SPEND_USD),
                window_start(now, _MONTH_S),
                cost.spend_usd,
                ttl_s=_MONTH_S + _DAY_S,
            )
        if cost.concurrent:
            await self._store.adjust_gauge(self._gauge_key(), cost.concurrent)
        return decision

    async def release(self, concurrent: int = 1) -> int:
        """Release ``concurrent`` held in-flight slots; return the new gauge value."""
        return await self._store.adjust_gauge(self._gauge_key(), -abs(concurrent))

    async def near_limit(self) -> Iterable[tuple[DimensionUsage, float]]:
        """Bounded dimensions whose current utilisation crossed an alert fraction.

        Yields ``(usage, fraction)`` for the *highest* alert fraction each
        dimension has crossed, so the governor fires at most one alert per
        dimension per evaluation.
        """
        decision = await self.utilisation()
        fractions = sorted(self.limits.alert_fractions, reverse=True)
        hits: list[tuple[DimensionUsage, float]] = []
        for usage in decision.usages:
            if usage.unbounded:
                continue
            for frac in fractions:
                if usage.utilisation >= frac:
                    hits.append((usage, frac))
                    break
        return hits


@dataclass
class QuotaRegistry:
    """Per-provider :class:`QuotaLimits`, with a default for unknown providers."""

    limits: dict[str, QuotaLimits] = field(default_factory=dict)
    default: QuotaLimits = field(default_factory=QuotaLimits)

    def for_provider(self, provider: str) -> QuotaLimits:
        return self.limits.get(provider, self.default)

    def set(self, provider: str, limits: QuotaLimits) -> None:
        self.limits[provider] = limits


__all__ = [
    "DimensionUsage",
    "QuotaAccountant",
    "QuotaDecision",
    "QuotaDimension",
    "QuotaLimits",
    "QuotaRegistry",
    "RenderCost",
    "window_start",
]
