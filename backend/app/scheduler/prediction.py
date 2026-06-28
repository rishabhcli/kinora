"""Per-reader reading-behaviour prediction (kinora.md §4.3/§4.6).

§4.3 gives the Scheduler a single online estimate — reading velocity ``v`` as an
EWMA of words-per-second, clamped to ``[0.5×, 3×]`` of the 4 wps default — and
§4.6 self-tunes promotion off it. That is enough to *react*, but the rest of this
domain (budget-optimal scheduling, adaptive watermarks, speculative rollback)
wants to *predict*: how fast will this reader be a minute from now, and how noisy
is that estimate?

This module is the prediction substrate. :class:`ReadingModel` is a pure,
deterministic, online estimator that ingests the same (focus-word, dt) samples
the §4.7 settle cadence already produces and maintains, per reader:

* an **EWMA velocity** ``v̂`` (the §4.3 estimate, but kept as a model so its decay
  is explicit and tunable) over a configurable half-life window;
* an **EWMA variance** of velocity — the reader's *consistency*. A metronomic
  reader has low variance; a stop-start reader has high variance. This is the
  signal :mod:`app.scheduler.adaptive` widens watermarks against, and it feeds the
  §4.6 stability gate without re-deriving it;
* a **dwell** estimate — the mean settle-window gap between intent updates, i.e.
  how long the reader lingers per position. Dwell is what separates a slow-but-
  steady reader (long dwell, low velocity) from a skimmer (short dwell, high
  velocity), and it is the horizon over which a §4.8 seek is *un*likely.

Everything here is pure given its inputs: no clock, no Redis, no network. The
model state is small and JSON-serialisable so it round-trips through the existing
:class:`~app.scheduler.model.SchedulerStore` Redis path. The estimator is *fed*
by the live :class:`~app.scheduler.intent.IntentController` and *replayed*
offline by the simulation harness (:mod:`app.scheduler.simulation`) — one model,
two drivers, identical math.

Crucially this changes **no spend behaviour**: the model only sharpens ``v`` and
exposes variance/dwell. Promotion stays gated on ``budget.can_render_live()``
exactly as before — a better velocity estimate cannot, by itself, spend a single
video-second the budget gate would not already allow.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.scheduler.zones import (
    DEFAULT_VELOCITY_WPS,
    VELOCITY_CLAMP_HIGH,
    VELOCITY_CLAMP_LOW,
    clamp_velocity,
)

#: EWMA half-life for the velocity estimate, in *samples* (§4.3's "10-second
#: window" at the ~200ms..2.5s settle cadence is a handful of samples; a 5-sample
#: half-life tracks that without chasing a single flick).
DEFAULT_VELOCITY_HALFLIFE = 5.0
#: Half-life for the dwell estimate (settle-gap between updates), in samples.
DEFAULT_DWELL_HALFLIFE = 5.0
#: Default dwell when no inter-update gap has been observed yet (the §4.7 settle
#: window): one debounce period.
DEFAULT_DWELL_MS = 200.0
#: A velocity coefficient-of-variation at/above this reads as an *unsteady*
#: reader (used by :meth:`ReadingModel.is_steady`); kept conservative so a merely
#: brisk-but-even reader is still "steady".
STEADY_CV_CEILING = 0.35


def _alpha_from_halflife(halflife_samples: float) -> float:
    """EWMA smoothing factor ``α`` for a given half-life in samples.

    ``α = 1 − 2^(−1/halflife)``: after ``halflife`` samples a step input has
    decayed halfway. A half-life ``<= 0`` collapses to ``α = 1`` (no memory).
    """
    if halflife_samples <= 0.0:
        return 1.0
    return 1.0 - math.pow(2.0, -1.0 / halflife_samples)


@dataclass(frozen=True, slots=True)
class VelocityPrediction:
    """A point estimate of reading velocity plus its uncertainty (§4.3/§4.6).

    ``mean_wps`` is the clamped EWMA used for ETA math (identical units to
    :attr:`SchedulerSession.velocity_wps`); ``raw_mean_wps`` is the pre-clamp
    estimate the §4.6 skim gate reads; ``std_wps`` is the EWMA standard deviation
    (the reader's velocity noise). ``coefficient_of_variation`` is ``std/mean`` —
    a unit-free steadiness measure — and ``samples`` is how many updates have
    informed the estimate (small counts are low-confidence).
    """

    mean_wps: float
    raw_mean_wps: float
    std_wps: float
    samples: int

    @property
    def coefficient_of_variation(self) -> float:
        """``std / mean`` — unit-free velocity noise (0 = metronomic)."""
        if self.mean_wps <= 0.0:
            return 0.0
        return self.std_wps / self.mean_wps


class ReadingModel(BaseModel):
    """A per-reader online estimator of velocity, its variance, and dwell (§4.3).

    Fed one :meth:`observe` call per settled intent update (the §4.7 cadence).
    Pure and deterministic: the same sample sequence always yields the same
    state, so the simulation harness can replay a trace and the live controller
    can drive it identically. JSON-serialisable (a Pydantic model) so it persists
    alongside the session control state.
    """

    #: EWMA of words-per-second (raw, *pre*-clamp — the skim gate reads this).
    velocity_ewma: float = DEFAULT_VELOCITY_WPS
    #: EWMA of velocity *variance* (Welford-style online, EWMA-decayed).
    velocity_var_ewma: float = 0.0
    #: EWMA of the inter-update settle gap in ms (the reader's dwell).
    dwell_ms_ewma: float = DEFAULT_DWELL_MS
    #: Count of observations folded in (confidence; small = cold-start).
    samples: int = 0

    # Smoothing factors are stored (not just the half-lives) so a model loaded
    # from Redis keeps the cadence it was trained at even if defaults change.
    velocity_alpha: float = Field(
        default_factory=lambda: _alpha_from_halflife(DEFAULT_VELOCITY_HALFLIFE)
    )
    dwell_alpha: float = Field(
        default_factory=lambda: _alpha_from_halflife(DEFAULT_DWELL_HALFLIFE)
    )

    @classmethod
    def with_halflives(
        cls,
        *,
        velocity_halflife: float = DEFAULT_VELOCITY_HALFLIFE,
        dwell_halflife: float = DEFAULT_DWELL_HALFLIFE,
        velocity_wps: float = DEFAULT_VELOCITY_WPS,
    ) -> ReadingModel:
        """Construct a cold model with explicit EWMA half-lives (in samples)."""
        return cls(
            velocity_ewma=velocity_wps,
            dwell_ms_ewma=DEFAULT_DWELL_MS,
            velocity_alpha=_alpha_from_halflife(velocity_halflife),
            dwell_alpha=_alpha_from_halflife(dwell_halflife),
        )

    # -- online update ------------------------------------------------------- #

    def observe(self, *, words_advanced: int, dt_ms: float) -> None:
        """Fold one settled intent update into the estimate (§4.3).

        ``words_advanced`` is the *signed* change in focus word since the last
        update (negative = a backward read; magnitude drives velocity). ``dt_ms``
        is the wall-clock gap since the last update — the dwell, and the divisor
        that turns words into a per-second rate.

        A zero/negative ``dt_ms`` (duplicate timestamp) is ignored: it carries no
        rate information and would divide by zero. A zero-word update (the reader
        held position across a settle window) still informs *dwell* but is not a
        velocity sample, so a thinking pause doesn't drag ``v`` toward zero and
        falsely look like a stall.
        """
        if dt_ms <= 0.0:
            return

        # Dwell always updates: even a hold tells us how long this reader lingers.
        self.dwell_ms_ewma = self._ewma(self.dwell_ms_ewma, dt_ms, self.dwell_alpha)

        if words_advanced == 0:
            # A pure dwell sample (no motion) — don't pull velocity toward 0.
            self.samples += 1
            return

        instant_wps = abs(words_advanced) / (dt_ms / 1000.0)
        prev_mean = self.velocity_ewma
        new_mean = self._ewma(prev_mean, instant_wps, self.velocity_alpha)
        # EWMA variance (incremental EWMA-variance): decay the old variance and
        # add the freshly-weighted squared deviation from the *previous* mean.
        # Stays non-negative and tracks a changing reader.
        deviation = instant_wps - prev_mean
        self.velocity_var_ewma = (1.0 - self.velocity_alpha) * (
            self.velocity_var_ewma + self.velocity_alpha * deviation * deviation
        )
        self.velocity_ewma = new_mean
        self.samples += 1

    @staticmethod
    def _ewma(prev: float, sample: float, alpha: float) -> float:
        return (1.0 - alpha) * prev + alpha * sample

    # -- prediction surface -------------------------------------------------- #

    def predict_velocity(self) -> VelocityPrediction:
        """The current velocity estimate + uncertainty (§4.3)."""
        raw = abs(self.velocity_ewma)
        std = math.sqrt(max(0.0, self.velocity_var_ewma))
        return VelocityPrediction(
            mean_wps=clamp_velocity(raw),
            raw_mean_wps=raw,
            std_wps=std,
            samples=self.samples,
        )

    def predict_dwell_ms(self) -> float:
        """Estimated time the reader lingers between settled updates (§4.7)."""
        return max(0.0, self.dwell_ms_ewma)

    def is_steady(self, *, cv_ceiling: float = STEADY_CV_CEILING) -> bool:
        """Whether the reader's velocity is consistent enough to trust ahead.

        Steady = a low coefficient of variation *and* a velocity inside the §4.3
        clamp band (a reader pinned at the skim ceiling is, by §4.6, not steady).
        Cold-start (``samples < 2``) is treated as steady so a brand-new session
        behaves exactly like the un-modelled default — no regression in spend.
        """
        if self.samples < 2:
            return True
        pred = self.predict_velocity()
        if pred.raw_mean_wps > VELOCITY_CLAMP_HIGH or pred.raw_mean_wps < VELOCITY_CLAMP_LOW:
            return False
        return pred.coefficient_of_variation < cv_ceiling

    def forecast_focus_word(self, focus_word: int, horizon_s: float) -> int:
        """Predict the focus word ``horizon_s`` of reading-time from now (§4.6).

        Uses the clamped mean velocity (the ETA-math velocity), so the forecast
        is consistent with how the zones classify shots. Forward-only: a reader's
        *expected* drift is forward even if the last sample was a backward glance
        (backward seeks are handled by §4.8, not predicted here).
        """
        v = self.predict_velocity().mean_wps
        return focus_word + int(round(max(0.0, v) * max(0.0, horizon_s)))


__all__ = [
    "DEFAULT_DWELL_HALFLIFE",
    "DEFAULT_DWELL_MS",
    "DEFAULT_VELOCITY_HALFLIFE",
    "STEADY_CV_CEILING",
    "ReadingModel",
    "VelocityPrediction",
]
