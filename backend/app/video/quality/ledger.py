"""Per-provider quality LEDGER — a rolling, decaying reputation per video model.

A single clip score is noisy; provider *selection* wants a stable reputation that
tracks each model's quality **over time** and forgets stale evidence (a provider that
fixes a regression should recover; one that degrades should decay). This ledger
aggregates :class:`~app.video.quality.scores.QualityScore` records per provider into:

* an **exponentially-weighted moving average** of the aggregate score, so recent
  clips count more — the EWMA decay factor derives from a configurable *half-life in
  observations* (``alpha = 1 − 2^(−1/half_life)``), a more intuitive knob than a raw α;
* per-axis EWMAs (the same six axes), so the leaderboard can say *why* a provider
  ranks where it does (great motion, weak identity, …);
* a flag rate (fraction of recent clips that were artifact/NSFW-flagged), tracked as
  its own EWMA so a provider that starts emitting garbage is penalised quickly;
* sample count + last-updated marker for confidence weighting.

The reputation exposed to the router (:meth:`ProviderReputation.reputation`) is the
score EWMA, *discounted* by the flag-rate and by a low-confidence shrink toward a
neutral prior until enough samples accrue — so a provider with one lucky clip can't
top the board. Pure + deterministic: an optional injected clock makes ``last_updated``
testable; with no clock it omits timestamps entirely.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from .scores import QualityScore, clamp01

_AXES = (
    "technical_integrity",
    "aesthetic",
    "prompt_adherence",
    "identity_consistency",
    "style_consistency",
    "motion_naturalness",
)
#: Neutral prior the EWMA shrinks toward while under-sampled (confidence shrink).
_NEUTRAL_PRIOR = 0.5
#: Samples below which the reputation is shrunk toward the neutral prior.
_CONFIDENCE_FLOOR = 5


def alpha_from_half_life(half_life: float) -> float:
    """EWMA smoothing α for a half-life expressed in *observations*.

    After ``half_life`` updates, an old observation's weight has halved:
    ``alpha = 1 − 2^(−1/half_life)``. ``half_life <= 0`` ⇒ α = 1 (no memory).
    """
    if half_life <= 0:
        return 1.0
    return 1.0 - math.pow(2.0, -1.0 / half_life)


class ProviderReputation(BaseModel):
    """The rolling reputation for one provider (immutable snapshot)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    samples: int = 0
    score_ewma: float = Field(default=_NEUTRAL_PRIOR, ge=0.0, le=1.0)
    flag_rate_ewma: float = Field(default=0.0, ge=0.0, le=1.0)
    axes_ewma: Mapping[str, float] = Field(default_factory=dict)
    last_updated: float | None = None

    def reputation(self) -> float:
        """The router-facing 0..1 number: score EWMA, flag-discounted + confidence-shrunk.

        * a fully-flagged provider (flag_rate→1) is multiplied toward 0;
        * an under-sampled provider is blended toward the neutral prior so one lucky
          clip can't crown it.
        """
        flag_discount = 1.0 - clamp01(self.flag_rate_ewma)
        raw = clamp01(self.score_ewma) * flag_discount
        if self.samples >= _CONFIDENCE_FLOOR:
            return round(raw, 6)
        confidence = self.samples / _CONFIDENCE_FLOOR if _CONFIDENCE_FLOOR else 1.0
        shrunk = confidence * raw + (1.0 - confidence) * _NEUTRAL_PRIOR
        return round(clamp01(shrunk), 6)


@dataclass(slots=True)
class _MutableRep:
    """Internal mutable accumulator (the public face is :class:`ProviderReputation`)."""

    provider: str
    samples: int = 0
    score_ewma: float = _NEUTRAL_PRIOR
    flag_rate_ewma: float = 0.0
    axes_ewma: dict[str, float] = field(
        default_factory=lambda: dict.fromkeys(_AXES, _NEUTRAL_PRIOR)
    )
    last_updated: float | None = None


def _ewma_step(prev: float, value: float, alpha: float, first: bool) -> float:
    """One EWMA update; the first observation seeds the average exactly."""
    if first:
        return value
    return (1.0 - alpha) * prev + alpha * value


@dataclass(slots=True)
class QualityLedger:
    """Aggregates per-clip scores into a decaying per-provider reputation.

    ``half_life`` is in *observations* (default 20: the last ~20 clips dominate).
    ``clock`` is an optional ``() -> float`` injected for testable timestamps; absent
    ⇒ ``last_updated`` stays ``None``. Not thread-safe (single-writer by design — the
    benchmark runner / a per-worker reputation cache).
    """

    half_life: float = 20.0
    clock: Callable[[], float] | None = None
    _reps: dict[str, _MutableRep] = field(default_factory=dict, init=False)

    @property
    def alpha(self) -> float:
        return alpha_from_half_life(self.half_life)

    def record(self, score: QualityScore) -> ProviderReputation:
        """Fold one clip score into its provider's reputation; return the snapshot."""
        rep = self._reps.get(score.provider)
        first = rep is None
        if rep is None:
            rep = _MutableRep(provider=score.provider)
            self._reps[score.provider] = rep
        a = self.alpha
        rep.score_ewma = _ewma_step(rep.score_ewma, score.aggregate, a, first)
        rep.flag_rate_ewma = _ewma_step(
            rep.flag_rate_ewma, 1.0 if score.flagged else 0.0, a, first
        )
        axes = score.sub_scores.as_mapping()
        for axis in _AXES:
            rep.axes_ewma[axis] = _ewma_step(rep.axes_ewma[axis], axes[axis], a, first)
        rep.samples += 1
        if self.clock is not None:
            rep.last_updated = self.clock()
        return self.snapshot(score.provider)

    def record_many(self, scores: Iterable[QualityScore]) -> None:
        """Fold a batch of scores (in order — EWMA is order-sensitive)."""
        for score in scores:
            self.record(score)

    def snapshot(self, provider: str) -> ProviderReputation:
        """Immutable reputation snapshot for one provider (KeyError if unseen)."""
        rep = self._reps[provider]
        return ProviderReputation(
            provider=rep.provider,
            samples=rep.samples,
            score_ewma=round(clamp01(rep.score_ewma), 6),
            flag_rate_ewma=round(clamp01(rep.flag_rate_ewma), 6),
            axes_ewma={a: round(clamp01(rep.axes_ewma[a]), 6) for a in _AXES},
            last_updated=rep.last_updated,
        )

    def reputations(self) -> dict[str, ProviderReputation]:
        """All current reputations keyed by provider."""
        return {name: self.snapshot(name) for name in self._reps}

    def best(self) -> ProviderReputation | None:
        """The provider with the highest router-facing reputation (None if empty).

        Ties break by sample count (more evidence wins) then provider name (stable).
        """
        snaps = list(self.reputations().values())
        if not snaps:
            return None
        return max(snaps, key=lambda r: (r.reputation(), r.samples, r.provider))

    def ranked(self) -> list[ProviderReputation]:
        """All reputations sorted best-first (same key as :meth:`best`)."""
        return sorted(
            self.reputations().values(),
            key=lambda r: (r.reputation(), r.samples, r.provider),
            reverse=True,
        )
