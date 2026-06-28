"""Adaptive watermarks — tune ``L``/``H``/``C`` to a reader's variance (§4.5/§4.6).

The §4.5 watermarks are constants — ``L=25s``, ``H=75s``, ``C=45s`` — chosen for a
*typical* reader. But §4.6 already makes the point that the system should
"self-tune to the reader": ETA divides by velocity, so a fast reader promotes
more and earlier *for free*. This module takes the next step the spec gestures at:
when a reader is **noisy** (high velocity variance), the safety margin a constant
watermark gives is too thin — a burst sized for a steady reader under-buffers a
reader who keeps surging. So we *widen* the buffer band for noisy readers and
leave a steady reader on the exact §4.5 constants.

The rule (pure, deterministic, bounded):

* **Low watermark ``L``** rises with the reader's velocity coefficient-of-variation
  (CV). A jittery reader needs to start refilling *sooner* — a bigger floor — so a
  velocity surge doesn't drain past the floor before the burst lands. Capped at a
  multiple of the base so it can never swallow the whole band.
* **High watermark ``H``** rises with predicted velocity *and* CV, so a fast-or-
  noisy reader gets a deeper buffer to coast on. Always kept ``> L`` by a minimum
  band so the hysteresis never collapses (which would re-introduce thrash).
* **Commit horizon ``C``** rises modestly with CV: a noisy reader's near-future
  ETA is itself uncertain, so we are slightly more willing to commit a shot that
  *might* fall just inside the horizon. Bounded below ``H`` (you never commit
  past where you'd stop buffering).

Safety: every output is clamped so it is **never smaller** than the base value and
the band ``L < C < H`` invariant always holds. Adaptation can only make the buffer
*safer* (deeper), never thinner — so it cannot increase stall risk, and because it
only changes *when* the existing budget-gated fill fires, it spends **no extra
video-seconds the budget gate would not already allow** (§4.6 promotion is still
``can_render_live()``-gated downstream).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.scheduler.prediction import ReadingModel
from app.scheduler.zones import DEFAULT_VELOCITY_WPS

#: How aggressively ``L`` widens per unit of velocity CV (dimensionless gain).
DEFAULT_LOW_CV_GAIN = 1.0
#: How aggressively ``H`` widens per unit of CV.
DEFAULT_HIGH_CV_GAIN = 1.5
#: How aggressively ``H`` widens with predicted velocity above the default.
DEFAULT_HIGH_VELOCITY_GAIN = 0.5
#: How aggressively ``C`` widens per unit of CV.
DEFAULT_COMMIT_CV_GAIN = 0.5
#: Ceilings: no watermark may exceed this multiple of its base (bounded growth).
DEFAULT_MAX_MULTIPLE = 2.0
#: The minimum hysteresis band ``H − L`` (seconds) — never collapse to thrash.
MIN_BAND_S = 20.0


@dataclass(frozen=True, slots=True)
class Watermarks:
    """A tuned ``(L, H, C)`` triple in seconds of reading-time (§4.5).

    Invariant (enforced by :func:`adapt_watermarks`): ``0 < L < C < H`` and each
    value is ``>=`` its base (adaptation only deepens the buffer).
    """

    low_s: float
    high_s: float
    commit_horizon_s: float

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.low_s, self.high_s, self.commit_horizon_s)


@dataclass(frozen=True, slots=True)
class AdaptiveConfig:
    """Tunable gains for the adaptive rule (all default to the constants above)."""

    low_cv_gain: float = DEFAULT_LOW_CV_GAIN
    high_cv_gain: float = DEFAULT_HIGH_CV_GAIN
    high_velocity_gain: float = DEFAULT_HIGH_VELOCITY_GAIN
    commit_cv_gain: float = DEFAULT_COMMIT_CV_GAIN
    max_multiple: float = DEFAULT_MAX_MULTIPLE
    min_band_s: float = MIN_BAND_S


def base_watermarks(settings: Settings | None = None) -> Watermarks:
    """The §4.5 constant watermarks from settings (the un-adapted baseline)."""
    s = settings or get_settings()
    return Watermarks(
        low_s=s.watermark_low_s,
        high_s=s.watermark_high_s,
        commit_horizon_s=s.commit_horizon_s,
    )


def adapt_watermarks(
    base: Watermarks,
    model: ReadingModel,
    *,
    config: AdaptiveConfig | None = None,
) -> Watermarks:
    """Widen ``base`` watermarks for a noisy/fast reader (§4.5/§4.6).

    Pure function of the base triple and the reader model. Returns watermarks that
    are **never smaller** than ``base`` and always satisfy ``L < C < H`` with at
    least ``config.min_band_s`` of hysteresis band.

    A cold-start model (``< 2`` samples) returns ``base`` unchanged, so a new
    session behaves byte-for-byte like the constant-watermark baseline.
    """
    config = config or AdaptiveConfig()
    pred = model.predict_velocity()
    if pred.samples < 2:
        return base

    cv = pred.coefficient_of_variation
    # Velocity headroom above the default, as a fraction (>= 0; clamped velocity
    # so a skimmer doesn't blow this up without bound — the §4.6 skim gate handles
    # skimmers separately by suspending promotion).
    vel_excess = max(0.0, (pred.mean_wps - DEFAULT_VELOCITY_WPS) / DEFAULT_VELOCITY_WPS)

    low = base.low_s * (1.0 + config.low_cv_gain * cv)
    high = base.high_s * (
        1.0 + config.high_cv_gain * cv + config.high_velocity_gain * vel_excess
    )
    commit = base.commit_horizon_s * (1.0 + config.commit_cv_gain * cv)

    # Bounded growth: never exceed max_multiple × base, never below base.
    low = _clamp(low, base.low_s, base.low_s * config.max_multiple)
    high = _clamp(high, base.high_s, base.high_s * config.max_multiple)
    commit = _clamp(commit, base.commit_horizon_s, base.commit_horizon_s * config.max_multiple)

    # Enforce the band + ordering invariants (L < C < H).
    high = max(high, low + config.min_band_s)
    commit = _clamp(commit, low + 1.0, high - 1.0)
    return Watermarks(low_s=round(low, 4), high_s=round(high, 4), commit_horizon_s=round(commit, 4))


def _clamp(value: float, low: float, high: float) -> float:
    if high < low:
        high = low
    return max(low, min(high, value))


__all__ = [
    "DEFAULT_COMMIT_CV_GAIN",
    "DEFAULT_HIGH_CV_GAIN",
    "DEFAULT_HIGH_VELOCITY_GAIN",
    "DEFAULT_LOW_CV_GAIN",
    "DEFAULT_MAX_MULTIPLE",
    "MIN_BAND_S",
    "AdaptiveConfig",
    "Watermarks",
    "adapt_watermarks",
    "base_watermarks",
]
