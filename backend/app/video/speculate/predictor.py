"""Reach predictor: which upcoming shots will the reader hit, and how likely (§4.6).

The reader is not a metronome reading strictly forward. From any position the
next minute branches:

* **linear** — they keep advancing at velocity ``v`` into the contiguous shots
  ahead. The chance of reaching a shot decays with how far ahead it is relative
  to the distance they will plausibly cover before it would expire.
* **jump** — they flip to a chapter head / bookmark / TOC entry. Jumps are
  discrete, low-base-rate, and *more* likely when the reader dwells (a long
  pause precedes a deliberate navigation) and *less* likely mid-flow.
* **reread** — a backward glance into an already-buffered span (handled as a
  cache concern, not pre-rendered fresh here).

This module turns a :class:`ReaderState` + a list of :class:`UpcomingShot` into a
list of :class:`PredictedReach`, each with a calibrated ``hit_probability`` and an
``eta_s``. The math is deterministic and pure: no clock, no RNG. Probabilities are
*proper* (a shot's total hit-prob across the linear and jump paths is clamped to
``[0, 1]``), so the downstream EV planner can multiply them by value/cost safely.

Calibration is exposed as a frozen :class:`ReachModel` of tunables so the
accounting feedback loop can sharpen the linear decay / jump base-rate against the
realised hit/waste record (a reader who keeps overshooting our linear horizon
should make us *more* aggressive ahead; a reader who keeps jumping should shift
probability mass onto jump targets).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from app.video.speculate.types import (
    PathKind,
    PredictedReach,
    ReaderState,
    UpcomingShot,
)

#: Below this hit-probability a reach is not worth even offering to the planner —
#: it would never clear the EV bar and only adds noise.
DEFAULT_MIN_HIT_PROBABILITY = 0.02


@dataclass(frozen=True, slots=True)
class ReachModel:
    """Calibration for the reach predictor (tuned by the accounting loop).

    Attributes:
        linear_horizon_s: reading-seconds ahead at which the linear hit-probability
            has decayed to ~37% (the exponential decay scale). Larger ⇒ we believe
            the reader will reach further ahead ⇒ more aggressive speculation.
        jump_base_rate: baseline ``P(the reader jumps at all)`` over the horizon —
            split across the available jump targets.
        jump_dwell_ms_scale: dwell at which the jump-rate multiplier reaches ~1;
            longer dwell than this scales the jump rate up (a deliberate pause).
        unsteady_linear_penalty: factor (<1) applied to linear hit-probabilities
            when the reader is *unsteady* (§4.6 skim gate) — their forecast is
            noisy so we trust it less and lean toward not over-committing.
        min_hit_probability: floor below which a reach is dropped.
    """

    linear_horizon_s: float = 60.0
    jump_base_rate: float = 0.15
    jump_dwell_ms_scale: float = 1500.0
    unsteady_linear_penalty: float = 0.6
    min_hit_probability: float = DEFAULT_MIN_HIT_PROBABILITY

    def with_aggressiveness(self, factor: float) -> ReachModel:
        """Return a copy scaled by ``factor`` (the accounting-loop knob).

        ``factor > 1`` widens the linear horizon (reach further) and lifts the
        jump base-rate; ``factor < 1`` pulls both in. Clamped so the model can
        never go non-positive. The penalty/floor are left untouched — they are
        safety rails, not aggressiveness.
        """
        f = max(0.1, min(4.0, factor))
        return ReachModel(
            linear_horizon_s=self.linear_horizon_s * f,
            jump_base_rate=min(0.9, self.jump_base_rate * f),
            jump_dwell_ms_scale=self.jump_dwell_ms_scale,
            unsteady_linear_penalty=self.unsteady_linear_penalty,
            min_hit_probability=self.min_hit_probability,
        )


def _eta_seconds(word_start: int, focus_word: int, velocity_wps: float) -> float:
    """Reading-seconds until ``word_start``: forward distance / velocity, ``>=0``."""
    v = max(velocity_wps, 1e-3)
    return max(0.0, (word_start - focus_word) / v)


def _linear_hit_probability(eta_s: float, model: ReachModel, *, steady: bool) -> float:
    """Exponential-decay linear reach probability over ETA.

    A shot the reader is about to enter (ETA→0) is near-certain; the probability
    decays as ``exp(-eta / horizon)``. An unsteady reader's linear forecast is
    discounted by :attr:`ReachModel.unsteady_linear_penalty`.
    """
    horizon = max(model.linear_horizon_s, 1e-3)
    p = math.exp(-eta_s / horizon)
    if not steady:
        p *= model.unsteady_linear_penalty
    return p


def _jump_rate_multiplier(dwell_ms: float, model: ReachModel) -> float:
    """How dwell scales the jump base-rate (a long pause precedes navigation).

    Saturates: ``dwell / scale`` clamped to ``[0, 2]`` so even a very long pause
    at most doubles the base jump-rate (a reader who walks away is handled by the
    idle sweep, not by speculation).
    """
    scale = max(model.jump_dwell_ms_scale, 1e-3)
    return max(0.0, min(2.0, dwell_ms / scale))


class ReachPredictor:
    """Turns a reader snapshot + upcoming shots into probability-weighted reaches.

    Pure and deterministic. Construction takes a :class:`ReachModel`; the engine
    swaps in a freshly-tuned model as the accounting loop learns the reader.
    """

    def __init__(self, model: ReachModel | None = None) -> None:
        self._model = model or ReachModel()

    @property
    def model(self) -> ReachModel:
        return self._model

    def predict(
        self,
        state: ReaderState,
        upcoming: Sequence[UpcomingShot],
    ) -> list[PredictedReach]:
        """Predict the probability-weighted set of reaches from ``state``.

        Linear paths cover the contiguous forward shots; jump paths cover the
        shots flagged :attr:`UpcomingShot.is_jump_target`. A shot reachable by
        *both* a linear advance and a jump gets the union of the two independent
        probabilities (``1 − (1−p_lin)(1−p_jump)``). Reaches below the model floor
        are dropped. Returned sorted by descending hit-probability (stable, so
        equal-prob shots keep document order) for predictable downstream ranking.
        """
        forward = [s for s in upcoming if s.word_start >= state.focus_word]
        jump_targets = [s for s in forward if s.is_jump_target]
        n_jumps = len(jump_targets)
        jump_mult = _jump_rate_multiplier(state.dwell_ms, self._model)
        # Total jump mass over the horizon, split evenly across discrete targets.
        per_jump = (
            (self._model.jump_base_rate * jump_mult) / n_jumps if n_jumps else 0.0
        )

        reaches: list[PredictedReach] = []
        for shot in forward:
            eta = _eta_seconds(shot.word_start, state.focus_word, state.velocity_wps)
            p_lin = _linear_hit_probability(eta, self._model, steady=state.steady)
            p_jump = per_jump if shot.is_jump_target else 0.0
            # Independent-path union, then clamp to a proper probability.
            p = 1.0 - (1.0 - min(1.0, p_lin)) * (1.0 - min(1.0, p_jump))
            p = max(0.0, min(1.0, p))
            if p < self._model.min_hit_probability:
                continue
            kind = (
                PathKind.JUMP
                if (p_jump >= p_lin and shot.is_jump_target)
                else PathKind.LINEAR
            )
            reaches.append(
                PredictedReach(shot=shot, kind=kind, hit_probability=p, eta_s=eta)
            )

        reaches.sort(key=lambda r: (-r.hit_probability, r.shot.word_start))
        return reaches


__all__ = [
    "DEFAULT_MIN_HIT_PROBABILITY",
    "ReachModel",
    "ReachPredictor",
]
