"""Hit/waste accounting + aggressiveness tuning feedback loop (kinora.md §4.6).

Speculation is a bet. Over time we observe whether each speculative shot was a
**hit** (the reader actually reached it before it expired — the spend paid off) or
a **waste** (the reader's path diverged and we cancelled/expired it — spend lost).
This module is the closed loop that turns that record into a single
**aggressiveness** knob and feeds it back to the reach predictor:

* a high realised **hit-rate** *and* spare budget ⇒ we were too timid — the reader
  keeps reaching past where we speculated, so widen the horizon (be more
  aggressive ahead);
* a high realised **waste-rate** (lots of cancelled long-shots) ⇒ we were too
  greedy — pull the horizon in.

The tuner is a bounded, EWMA-smoothed proportional controller so a single unlucky
seek doesn't whipsaw the policy. It is **pure**: it consumes recorded outcomes and
emits a multiplier; the engine applies that to :meth:`ReachModel.with_aggressiveness`.
Crucially it can only *re-shape* speculation under the existing spend cap — the
budget seam is still the hard ceiling, so a runaway tuner can never spend past it.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

#: EWMA half-life (in recorded outcomes) for the hit-rate / waste-rate estimates.
DEFAULT_OUTCOME_HALFLIFE = 8.0
#: Aggressiveness multiplier clamp band — the predictor's horizon can at most
#: quadruple or quarter relative to its baseline, no matter how skewed the record.
MIN_AGGRESSIVENESS = 0.25
MAX_AGGRESSIVENESS = 4.0
#: Hit-rate at/above which (with spare budget) we lean *more* aggressive.
DEFAULT_HIT_TARGET = 0.6
#: Realised-waste-fraction-of-spend at/above which we pull *in*.
DEFAULT_WASTE_CEILING = 0.4


def _alpha(halflife: float) -> float:
    """EWMA smoothing factor for a half-life in samples (matches scheduler math)."""
    if halflife <= 0.0:
        return 1.0
    return 1.0 - 2.0 ** (-1.0 / halflife)


class SpeculationStats(BaseModel):
    """Cumulative + smoothed hit/waste record for one reading session (serialisable).

    Counts are lifetime totals (for reporting); the EWMA fields are what the tuner
    reads (recency-weighted, so the policy tracks the *current* reader, not their
    whole history).
    """

    model_config = {"validate_assignment": True}

    launched: int = Field(default=0, ge=0)
    hits: int = Field(default=0, ge=0)
    wastes: int = Field(default=0, ge=0)
    spent_usd: float = Field(default=0.0, ge=0.0)
    hit_value: float = Field(default=0.0, ge=0.0)
    wasted_usd: float = Field(default=0.0, ge=0.0)
    refunded_usd: float = Field(default=0.0, ge=0.0)

    #: EWMA of the per-outcome hit indicator (1.0 hit / 0.0 waste).
    ewma_hit_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    #: EWMA of the per-outcome wasted-fraction (wasted_usd / cost for that shot).
    ewma_waste_fraction: float = Field(default=0.0, ge=0.0, le=1.0)
    samples: int = Field(default=0, ge=0)

    @property
    def realised_roi(self) -> float:
        """Hit value per realised dollar (spent − refunded). 0 when nothing spent."""
        net = self.spent_usd - self.refunded_usd
        if net <= 1e-9:
            return 0.0
        return round(self.hit_value / net, 6)


@dataclass(frozen=True, slots=True)
class TunerPolicy:
    """Tunables for :class:`AggressivenessTuner` (deterministic)."""

    outcome_halflife: float = DEFAULT_OUTCOME_HALFLIFE
    hit_target: float = DEFAULT_HIT_TARGET
    waste_ceiling: float = DEFAULT_WASTE_CEILING
    #: Proportional gain: how hard a hit/waste delta moves the multiplier.
    gain: float = 0.5


class SpeculationAccountant:
    """Records hit/waste outcomes and tunes the aggressiveness multiplier (pure).

    One accountant per reading session. ``record_hit`` / ``record_waste`` fold each
    realised outcome into the stats; :meth:`aggressiveness` reads the smoothed
    record and returns the bounded multiplier the engine applies to the predictor.
    """

    def __init__(
        self,
        policy: TunerPolicy | None = None,
        stats: SpeculationStats | None = None,
    ) -> None:
        self._policy = policy or TunerPolicy()
        self._stats = stats or SpeculationStats()
        self._alpha = _alpha(self._policy.outcome_halflife)

    @property
    def stats(self) -> SpeculationStats:
        return self._stats

    # -- recording --------------------------------------------------------- #

    def record_launch(self, cost_usd: float) -> None:
        """Note a speculation went out the door (for launched/spent totals)."""
        self._stats.launched += 1
        self._stats.spent_usd = round(self._stats.spent_usd + max(0.0, cost_usd), 6)

    def record_hit(self, *, cost_usd: float, value: float) -> None:
        """The reader reached a speculated shot — the bet paid off."""
        self._stats.hits += 1
        self._stats.hit_value = round(self._stats.hit_value + max(0.0, value), 6)
        self._fold_outcome(hit=True, wasted_fraction=0.0)

    def record_waste(self, *, cost_usd: float, refunded_usd: float = 0.0) -> None:
        """A speculation was cancelled/expired unreached — spend lost (minus refund).

        ``refunded_usd`` (an unstarted reservation returned to the budget) is *not*
        counted as waste — only the sunk portion is. A fully-refunded cancellation
        therefore costs zero waste, exactly as the budget reflects.
        """
        self._stats.wastes += 1
        sunk = max(0.0, cost_usd - max(0.0, refunded_usd))
        self._stats.wasted_usd = round(self._stats.wasted_usd + sunk, 6)
        self._stats.refunded_usd = round(
            self._stats.refunded_usd + max(0.0, refunded_usd), 6
        )
        frac = 0.0 if cost_usd <= 1e-9 else min(1.0, sunk / cost_usd)
        self._fold_outcome(hit=False, wasted_fraction=frac)

    def _fold_outcome(self, *, hit: bool, wasted_fraction: float) -> None:
        a = self._alpha
        hit_ind = 1.0 if hit else 0.0
        self._stats.ewma_hit_rate = round(
            (1 - a) * self._stats.ewma_hit_rate + a * hit_ind, 6
        )
        self._stats.ewma_waste_fraction = round(
            (1 - a) * self._stats.ewma_waste_fraction + a * wasted_fraction, 6
        )
        self._stats.samples += 1

    # -- tuning ------------------------------------------------------------ #

    def aggressiveness(self, *, budget_utilisation: float = 0.0) -> float:
        """The bounded multiplier the engine feeds to the reach predictor (§4.6).

        ``budget_utilisation`` ∈ ``[0, 1]`` is how much of the speculative cap is
        currently committed; a high hit-rate only justifies *more* aggression when
        there is spare budget to fund it (low utilisation). Cold-start (``samples
        < 2``) returns a neutral ``1.0`` so a fresh session behaves like the
        baseline — no spend regression.
        """
        if self._stats.samples < 2:
            return 1.0
        p = self._policy
        # Reward: how far hit-rate is over target, gated by spare budget.
        spare = max(0.0, 1.0 - max(0.0, min(1.0, budget_utilisation)))
        reward = (self._stats.ewma_hit_rate - p.hit_target) * spare
        # Penalty: how far waste-fraction is over its ceiling.
        penalty = self._stats.ewma_waste_fraction - p.waste_ceiling
        delta = p.gain * (reward - penalty)
        multiplier = 1.0 + delta
        return max(MIN_AGGRESSIVENESS, min(MAX_AGGRESSIVENESS, multiplier))


__all__ = [
    "DEFAULT_HIT_TARGET",
    "DEFAULT_OUTCOME_HALFLIFE",
    "DEFAULT_WASTE_CEILING",
    "MAX_AGGRESSIVENESS",
    "MIN_AGGRESSIVENESS",
    "SpeculationAccountant",
    "SpeculationStats",
    "TunerPolicy",
]
