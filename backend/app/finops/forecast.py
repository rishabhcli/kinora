"""Cost forecasting from a reading trajectory (kinora.md §11.1, §4.4, §4.6).

Video-seconds are spent only when a beat the reader is *arriving at* crosses the
commit horizon and is promoted to full video (§4.6); speculation is free (§4.4).
So the forward video cost of a reading session is, to first order, a function of:

* how fast the reader is moving (``velocity_wps``) and how many words remain;
* the *density* of promotable shots ahead (seconds of shot per word of text);
* the fraction of arriving shots that are actually promoted to full video
  (``promotion_rate`` — a fast skimmer promotes little, §4.6) and the realistic
  regeneration overhead (a fraction of shots are re-rendered, §11.1 ≈ 20%).

This module is pure math over those signals. It produces:

* :func:`forecast_video_seconds` — expected forward video-seconds over a horizon;
* :class:`BurnDown` — a sampled burn-down curve of *remaining* budget vs. time;
* :func:`seconds_to_exhaustion` — wall-clock ETA until a cap is hit at the current
  burn rate (``inf`` if the rate is zero / the budget never runs out).

It never raises on ordinary input and performs no I/O — the live signals (used
seconds, velocity, remaining words) are passed in by the governor/scheduler.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: A realistic regeneration overhead (§11.1: "~20% regeneration rate"): each
#: accepted shot-second costs a little more in practice because some renders are
#: rejected by the Critic and re-run. Expressed as a multiplier on raw seconds.
DEFAULT_REGEN_OVERHEAD = 0.20

#: Seconds of full-video shot produced per word of text, on average. A scene is
#: a handful of ~5s shots over a few hundred words; ~0.02 s/word is a reasonable
#: default density and is overridable per book once Phase-A shot planning is in.
DEFAULT_SHOT_SECONDS_PER_WORD = 0.02


@dataclass(frozen=True, slots=True)
class ReadingTrajectory:
    """The live signals a forecast reads — all already known to the scheduler.

    Attributes:
        velocity_wps: words/second the reader is moving forward (>= 0).
        words_remaining: words left before the end of the book/section.
        shot_seconds_per_word: video-seconds of promotable shot per word ahead.
        promotion_rate: fraction of arriving shots promoted to full video [0, 1].
        regen_overhead: extra fraction re-rendered on top of accepted seconds.
    """

    velocity_wps: float
    words_remaining: int
    shot_seconds_per_word: float = DEFAULT_SHOT_SECONDS_PER_WORD
    promotion_rate: float = 1.0
    regen_overhead: float = DEFAULT_REGEN_OVERHEAD

    @property
    def reading_seconds_remaining(self) -> float:
        """Wall-clock reading time left at the current velocity (``inf`` if idle)."""
        if self.velocity_wps <= 0.0:
            return math.inf
        return max(self.words_remaining, 0) / self.velocity_wps

    @property
    def video_seconds_per_reading_second(self) -> float:
        """The burn *rate*: video-seconds spent per real reading-second.

        ``velocity * shot_seconds_per_word`` is the raw seconds-of-shot the reader
        sweeps past each second; scaled by the promotion rate (only promoted shots
        cost video) and inflated by the regeneration overhead.
        """
        raw = self.velocity_wps * max(self.shot_seconds_per_word, 0.0)
        return raw * _clamp01(self.promotion_rate) * (1.0 + max(self.regen_overhead, 0.0))


def _clamp01(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def forecast_video_seconds(traj: ReadingTrajectory, *, horizon_s: float) -> float:
    """Expected forward video-seconds over ``horizon_s`` of reading time.

    Bounded by how much *reading* actually remains: a reader near the end of the
    book cannot spend a full horizon's worth of seconds. Idle readers (velocity
    0) spend nothing.
    """
    if horizon_s <= 0.0 or traj.velocity_wps <= 0.0:
        return 0.0
    effective_s = min(horizon_s, traj.reading_seconds_remaining)
    return traj.video_seconds_per_reading_second * effective_s


@dataclass(frozen=True, slots=True)
class BurnSample:
    """One point on a burn-down curve: remaining budget at a moment in time."""

    t_s: float
    spent_s: float
    remaining_s: float

    def as_dict(self) -> dict[str, float]:
        return {
            "t_s": round(self.t_s, 3),
            "spent_s": round(self.spent_s, 3),
            "remaining_s": round(self.remaining_s, 3),
        }


@dataclass(frozen=True, slots=True)
class BurnDown:
    """A sampled forecast of remaining video-budget over a reading horizon."""

    samples: tuple[BurnSample, ...]
    burn_rate_s_per_s: float
    exhaust_at_s: float

    @property
    def will_exhaust(self) -> bool:
        """True if the budget is forecast to hit zero within the sampled horizon."""
        return math.isfinite(self.exhaust_at_s) and bool(self.samples) and (
            self.exhaust_at_s <= self.samples[-1].t_s
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "burn_rate_s_per_s": round(self.burn_rate_s_per_s, 5),
            "exhaust_at_s": None if math.isinf(self.exhaust_at_s) else round(self.exhaust_at_s, 2),
            "will_exhaust": self.will_exhaust,
            "samples": [s.as_dict() for s in self.samples],
        }


def seconds_to_exhaustion(*, remaining_s: float, burn_rate_s_per_s: float) -> float:
    """Wall-clock seconds until ``remaining_s`` reaches 0 at ``burn_rate_s_per_s``.

    ``inf`` when the burn rate is non-positive (idle / never exhausts) or nothing
    remains-to-burn beyond zero. A budget already at/under zero exhausts at ``0``.
    """
    if remaining_s <= 0.0:
        return 0.0
    if burn_rate_s_per_s <= 0.0:
        return math.inf
    return remaining_s / burn_rate_s_per_s


def burn_down(
    traj: ReadingTrajectory,
    *,
    remaining_s: float,
    horizon_s: float,
    steps: int = 12,
) -> BurnDown:
    """Sample the remaining-budget burn-down over ``horizon_s`` of reading.

    The burn rate is constant in this first-order model (velocity-driven), so the
    curve is a straight ramp down — but it is *clamped at zero* (the budget can't
    go negative; the hard cap would have refused the reservation) and the samples
    stop accruing spend once reading ends. The exhaustion ETA is computed from the
    same rate.
    """
    steps = max(steps, 1)
    rate = traj.video_seconds_per_reading_second
    reading_left = traj.reading_seconds_remaining
    samples: list[BurnSample] = []
    for i in range(steps + 1):
        t = horizon_s * i / steps
        active_t = min(t, reading_left)
        spent = min(rate * active_t, max(remaining_s, 0.0))
        samples.append(
            BurnSample(t_s=t, spent_s=spent, remaining_s=max(remaining_s - spent, 0.0))
        )
    exhaust = seconds_to_exhaustion(remaining_s=remaining_s, burn_rate_s_per_s=rate)
    # Beyond the end of reading the budget stops draining, so an ETA past the
    # reading horizon is effectively "never within this read".
    if math.isfinite(exhaust) and exhaust > reading_left:
        exhaust = math.inf
    return BurnDown(samples=tuple(samples), burn_rate_s_per_s=rate, exhaust_at_s=exhaust)


@dataclass(frozen=True, slots=True)
class ForecastReport:
    """The headline forecast a HUD / the governor reads."""

    horizon_s: float
    forecast_video_s: float
    remaining_s: float
    headroom_after_s: float
    burn: BurnDown

    @property
    def fits(self) -> bool:
        """True if the forecast forward spend fits within the remaining budget."""
        return self.forecast_video_s <= self.remaining_s + 1e-9

    def as_dict(self) -> dict[str, object]:
        return {
            "horizon_s": round(self.horizon_s, 2),
            "forecast_video_s": round(self.forecast_video_s, 3),
            "remaining_s": round(self.remaining_s, 3),
            "headroom_after_s": round(self.headroom_after_s, 3),
            "fits": self.fits,
            "burn": self.burn.as_dict(),
        }


def build_forecast(
    traj: ReadingTrajectory,
    *,
    remaining_s: float,
    horizon_s: float,
    steps: int = 12,
) -> ForecastReport:
    """Assemble the full :class:`ForecastReport` (forecast + burn-down + headroom)."""
    forecast = forecast_video_seconds(traj, horizon_s=horizon_s)
    burn = burn_down(traj, remaining_s=remaining_s, horizon_s=horizon_s, steps=steps)
    return ForecastReport(
        horizon_s=horizon_s,
        forecast_video_s=forecast,
        remaining_s=remaining_s,
        headroom_after_s=remaining_s - forecast,
        burn=burn,
    )


#: Default EWMA smoothing factor for velocity. Higher = more weight on the latest
#: sample (more responsive, noisier); lower = smoother, slower to react.
DEFAULT_EWMA_ALPHA = 0.4


class VelocityEstimator:
    """An exponentially-weighted moving average of reading velocity.

    Raw per-tick velocity is noisy (a reader pauses, skims a line, re-reads). A
    forecast built off a single noisy sample whipsaws; an EWMA gives a stable
    velocity to forecast against while still tracking real trend changes. The
    Scheduler already clamps velocity (§4.6) — this is the FinOps-side smoother
    that feeds :func:`build_forecast`.
    """

    def __init__(self, *, alpha: float = DEFAULT_EWMA_ALPHA, initial: float | None = None) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be within (0, 1]")
        self._alpha = alpha
        self._value: float | None = initial

    @property
    def value(self) -> float:
        """The current smoothed velocity (0.0 before the first sample)."""
        return self._value if self._value is not None else 0.0

    def update(self, sample_wps: float) -> float:
        """Fold in a fresh velocity sample; return the new smoothed value."""
        sample = max(sample_wps, 0.0)
        if self._value is None:
            self._value = sample
        else:
            self._value = self._alpha * sample + (1.0 - self._alpha) * self._value
        return self._value

    def reset(self, value: float | None = None) -> None:
        """Reset the estimator (e.g. after a seek — §4.8 resets velocity)."""
        self._value = value


__all__ = [
    "DEFAULT_EWMA_ALPHA",
    "DEFAULT_REGEN_OVERHEAD",
    "DEFAULT_SHOT_SECONDS_PER_WORD",
    "BurnDown",
    "BurnSample",
    "ForecastReport",
    "ReadingTrajectory",
    "VelocityEstimator",
    "build_forecast",
    "burn_down",
    "forecast_video_seconds",
    "seconds_to_exhaustion",
]
