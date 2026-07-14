"""The cost-&-usage *fact* every roll-up reads (kinora.md §11.1, §12.5).

This subsystem is a **time-series analytics warehouse over spend** — distinct from
the three neighbours it sits beside:

* :mod:`app.optim.cost_meter` is a *process-global accumulator* (one mutable
  rollup of "everything since boot"); it answers "what has this process spent?".
* :mod:`app.finops` is *budget governance* (tiered caps, forecast, the
  promote/optimize/halt decision); it answers "are we inside the cap right now?".
* :mod:`app.analytics` is the *product* event pipeline (reading behaviour,
  funnels, retention); it answers "how do humans use the product?".

This subsystem answers the **operator/FinOps-dashboard** question: *"over time,
binned by day/provider/model/book/session, what did we spend, how fast, how
reliably, and how good was the output — and is anything anomalous?"* It is a
**time-bucketed** store with downsampling/retention tiers, anomaly detection,
budget burndown + month-end forecast, and a read-only dashboard API.

A :class:`UsageEvent` is the one fact every producer speaks. It is a superset of
:class:`app.providers.types.Usage` (the physical spend) plus the dimensions and
quality/reliability signals a dashboard slices on. It carries no PII and no
prompt content — only counts, money, identifiers, and a quality score.
"""

from __future__ import annotations

import enum
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.optim.cost_meter import PRICING, Price, cost_of
from app.providers.types import Usage

_ZERO = Decimal("0")


class Provider(enum.StrEnum):
    """The cost source a model is attributed to (coarse, deterministic).

    The provider is inferred from the model id when not given explicitly, so a
    raw :class:`Usage` (which only carries a model id) can be ingested without the
    caller knowing the provider. Cross-provider cost normalization (Round-1) means
    every provider's spend lands in the same USD unit; this dimension is what lets
    a dashboard say *which* provider drove it.
    """

    DASHSCOPE = "dashscope"
    WAN = "wan"
    MINIMAX = "minimax"
    OPENAI = "openai"
    UNKNOWN = "unknown"


def infer_provider(model: str) -> Provider:
    """Map a model id to its provider (deterministic, total — never raises).

    Heuristic prefix match over the known fleet (AGENTS.md): ``wan*`` is the Wan
    video provider; ``qwen*`` / ``tongyi*`` is DashScope; ``minimax*`` /
    ``abab*`` / ``hailuo*`` is MiniMax; ``gpt*`` / ``o1`` / ``o3`` is OpenAI.
    Anything else is :attr:`Provider.UNKNOWN`.
    """
    m = model.strip().lower()
    if m.startswith(("wan", "wanx")):
        return Provider.WAN
    if m.startswith(("qwen", "tongyi")):
        return Provider.DASHSCOPE
    if m.startswith(("minimax", "abab", "hailuo", "speech-0", "video-0")):
        return Provider.MINIMAX
    if m.startswith(("gpt", "o1", "o3", "o4", "text-embedding")):
        return Provider.OPENAI
    return Provider.UNKNOWN


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """One billable provider call, enriched for time-series analytics.

    Attributes:
        at: When the call happened (timezone-aware UTC; naive is coerced to UTC).
        model: Model id the spend is attributed to.
        operation: Coarse op label (``chat``/``vl``/``image``/``tts``/``video``).
        provider: Cost source; inferred from ``model`` when omitted.
        book_id: Owning book, when known (attribution dimension).
        session_id: Owning reading session, when known.
        input_tokens / output_tokens / images / audio_seconds / video_seconds:
            The physical spend (same units as :class:`Usage`).
        latency_ms: Wall-clock latency, when measured (drives p50/p95).
        success: Whether the call succeeded (drives success-rate / error-surge).
        cache_hit: Whether the result was served from cache (drives cache-hit %).
        cost_usd: USD cost of the call. Filled from the price table at ingest when
            not supplied, so callers can pass a normalized cross-provider cost or
            let the engine price it.
        quality: Optional quality/CCS score in [0, 1] (drives the quality series
            and the regression anomaly). ``None`` means "no quality signal".
    """

    at: datetime
    model: str
    operation: str
    provider: Provider = Provider.UNKNOWN
    book_id: str | None = None
    session_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    images: int = 0
    audio_seconds: float = 0.0
    video_seconds: float = 0.0
    latency_ms: float | None = None
    success: bool = True
    cache_hit: bool = False
    cost_usd: Decimal = _ZERO
    quality: float | None = None

    @classmethod
    def from_usage(
        cls,
        usage: Usage,
        *,
        at: datetime,
        book_id: str | None = None,
        session_id: str | None = None,
        success: bool = True,
        cache_hit: bool = False,
        quality: float | None = None,
        cost_usd: Decimal | None = None,
        pricing: Mapping[str, Price] = PRICING,
        provider: Provider | None = None,
    ) -> UsageEvent:
        """Lift a raw :class:`Usage` into a :class:`UsageEvent`.

        Cost is priced from the table when ``cost_usd`` is not supplied (an
        unpriced model costs zero — never raises). The provider is inferred from
        the model id unless given.
        """
        return cls(
            at=_as_utc(at),
            model=usage.model,
            operation=usage.operation,
            provider=provider or infer_provider(usage.model),
            book_id=book_id,
            session_id=session_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            images=usage.images,
            audio_seconds=float(usage.audio_seconds),
            video_seconds=float(usage.video_seconds),
            latency_ms=usage.latency_ms,
            success=success,
            cache_hit=cache_hit,
            cost_usd=cost_of(usage, pricing) if cost_usd is None else cost_usd,
            quality=quality,
        )

    def __post_init__(self) -> None:
        # Normalise timestamp + provider + cost without breaking frozen semantics.
        object.__setattr__(self, "at", _as_utc(self.at))
        if self.provider is Provider.UNKNOWN:
            object.__setattr__(self, "provider", infer_provider(self.model))
        if not isinstance(self.cost_usd, Decimal):
            object.__setattr__(self, "cost_usd", Decimal(str(self.cost_usd)))


def _as_utc(at: datetime) -> datetime:
    """Coerce a datetime to timezone-aware UTC (naive → assumed UTC)."""
    if at.tzinfo is None:
        return at.replace(tzinfo=UTC)
    return at.astimezone(UTC)


# --------------------------------------------------------------------------- #
# The accumulating cell — the unit a roll-up bucket holds
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class MetricCell:
    """A mutable accumulator of the metrics for one (bucket × dimension) slice.

    Holds running sums plus the bounded inputs needed for derived metrics:
    latency samples (for p50/p95), success/error counts, cache hits, and a
    quality sum/count. ``merge`` folds another cell in (used by downsampling and
    cross-dimension totals). All math is pure; nothing raises.
    """

    #: Cap on retained latency samples per cell so a hot bucket can't grow
    #: unbounded; the first N samples in a bucket are representative for p50/p95.
    MAX_LATENCY_SAMPLES: int = field(default=4096, repr=False)

    calls: int = 0
    errors: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    images: int = 0
    audio_seconds: float = 0.0
    video_seconds: float = 0.0
    cost_usd: Decimal = field(default_factory=lambda: _ZERO)
    quality_sum: float = 0.0
    quality_count: int = 0
    _latencies: list[float] = field(default_factory=list)

    def add(self, ev: UsageEvent) -> None:
        """Fold one event into the cell."""
        self.calls += 1
        if not ev.success:
            self.errors += 1
        if ev.cache_hit:
            self.cache_hits += 1
        self.input_tokens += ev.input_tokens
        self.output_tokens += ev.output_tokens
        self.images += ev.images
        self.audio_seconds += ev.audio_seconds
        self.video_seconds += ev.video_seconds
        self.cost_usd += ev.cost_usd
        if ev.latency_ms is not None and len(self._latencies) < self.MAX_LATENCY_SAMPLES:
            self._latencies.append(float(ev.latency_ms))
        if ev.quality is not None:
            self.quality_sum += ev.quality
            self.quality_count += 1

    def merge(self, other: MetricCell) -> None:
        """Fold another cell into this one (downsampling / cross-dim totals)."""
        self.calls += other.calls
        self.errors += other.errors
        self.cache_hits += other.cache_hits
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.images += other.images
        self.audio_seconds += other.audio_seconds
        self.video_seconds += other.video_seconds
        self.cost_usd += other.cost_usd
        room = self.MAX_LATENCY_SAMPLES - len(self._latencies)
        if room > 0 and other._latencies:
            self._latencies.extend(other._latencies[:room])
        self.quality_sum += other.quality_sum
        self.quality_count += other.quality_count

    def copy(self) -> MetricCell:
        """A deep-enough copy (the latency list is duplicated)."""
        c = MetricCell(
            calls=self.calls,
            errors=self.errors,
            cache_hits=self.cache_hits,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            images=self.images,
            audio_seconds=self.audio_seconds,
            video_seconds=self.video_seconds,
            cost_usd=self.cost_usd,
            quality_sum=self.quality_sum,
            quality_count=self.quality_count,
        )
        c._latencies = list(self._latencies)
        return c

    # --- derived metrics ---------------------------------------------------- #

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def success_rate(self) -> float:
        """Fraction of calls that succeeded in [0, 1] (1.0 when no calls)."""
        if self.calls == 0:
            return 1.0
        return (self.calls - self.errors) / self.calls

    @property
    def error_rate(self) -> float:
        """Fraction of calls that failed in [0, 1] (0.0 when no calls)."""
        if self.calls == 0:
            return 0.0
        return self.errors / self.calls

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of calls served from cache in [0, 1] (0.0 when no calls)."""
        if self.calls == 0:
            return 0.0
        return self.cache_hits / self.calls

    @property
    def avg_quality(self) -> float | None:
        """Mean quality score, or ``None`` when no event carried one."""
        if self.quality_count == 0:
            return None
        return self.quality_sum / self.quality_count

    def latency_percentile(self, q: float) -> float | None:
        """Nearest-rank percentile of retained latencies (``None`` when empty).

        ``q`` is a fraction in [0, 1] (0.5 → p50, 0.95 → p95).
        """
        return percentile(self._latencies, q)

    def as_dict(self) -> dict[str, Any]:
        """JSON-serializable view; ``cost_usd`` as a decimal *string*."""
        p50 = self.latency_percentile(0.5)
        p95 = self.latency_percentile(0.95)
        q = self.avg_quality
        return {
            "calls": self.calls,
            "errors": self.errors,
            "success_rate": round(self.success_rate, 6),
            "error_rate": round(self.error_rate, 6),
            "cache_hit_rate": round(self.cache_hit_rate, 6),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "images": self.images,
            "audio_seconds": round(self.audio_seconds, 3),
            "video_seconds": round(self.video_seconds, 3),
            "cost_usd": str(self.cost_usd),
            "latency_p50_ms": None if p50 is None else round(p50, 3),
            "latency_p95_ms": None if p95 is None else round(p95, 3),
            "avg_quality": None if q is None else round(q, 6),
        }


def percentile(samples: list[float], q: float) -> float | None:
    """Nearest-rank percentile of ``samples`` (``None`` for an empty list).

    Pure, total, and deterministic: ``q`` is clamped to [0, 1]; the rank is
    ``ceil(q * n)`` (1-based), so p100 → max, p0 → min.
    """
    if not samples:
        return None
    qq = 0.0 if q < 0.0 else 1.0 if q > 1.0 else q
    ordered = sorted(samples)
    n = len(ordered)
    if qq <= 0.0:
        return ordered[0]
    rank = max(1, math.ceil(qq * n))
    return ordered[min(rank, n) - 1]


__all__ = [
    "MetricCell",
    "Provider",
    "UsageEvent",
    "infer_provider",
    "percentile",
]
