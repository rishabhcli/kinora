"""Reader-velocity *regime* model + page-need prediction (kinora.md §4.3/§4.6).

The base :class:`~app.scheduler.prediction.ReadingModel` (§4.3) gives a single
scalar — an EWMA velocity with variance and dwell. That is enough to size ETA and
detect a skim with the §4.6 stability gate, but the adaptive layer wants a
*coarser, named* read on what the reader is **doing right now**, because each
behaviour wants a different scheduling posture:

* **STEADY** — metronomic forward reading. Promote ahead aggressively; the §4.5
  watermarks (or a modest widening) are perfect.
* **SKIMMING** — velocity above the §4.3 clamp ceiling. The §4.6 gate already
  suspends promotion; this regime makes that explicit so the watermark sizer
  *shrinks* the committed band (don't pre-render pages a skimmer blows past) and
  leans on the keyframe ladder.
* **REREADING** — a run of *backward* motion (re-reading a passage). Backward
  reads are essentially free from cache (§4.8 worked example), so the committed
  band can shrink and prefetch should bias *behind* the playhead.
* **PONDERING** — slow forward reading with long dwell (a thinker between idle
  pauses). Velocity is low, so ETAs are long; widen the band a little to ride out
  the next think without draining, and prefer keeping the buffer *full* over
  refilling in a burst (a thinker's burst is rare but the stall is jarring).
* **JUMPING** — large discontinuous focus jumps (§4.8 seeks). Promotion is
  pointless until the trajectory re-stabilises; the band collapses to the floor.

:class:`VelocityRegimeModel` wraps a base :class:`ReadingModel` and adds the small
amount of *directional* and *jump* state the scalar model deliberately omits
(it forecasts forward-only). It is pure, deterministic, and JSON-serialisable, fed
the same ``(words_advanced, dt_ms)`` samples — one model, two drivers (live
controller / offline simulator), identical math.

:func:`predict_pages_needed` turns the regime + velocity into the concrete output
the scheduler consumes: *which upcoming word-spans will need rendering inside the
commit horizon*, ordered by urgency. :func:`size_watermarks` is the regime-aware
watermark sizer — it composes with (does not replace) the existing
:func:`app.scheduler.adaptive.adapt_watermarks` variance widening.

**Spend invariant.** Nothing here promotes or reserves. It only sharpens *which*
shots the budget-gated fill should consider and *how deep* the buffer band is.
A SKIMMING/JUMPING regime can only make the band *thinner* (less speculative
spend); a PONDERING regime widens it but promotion stays ``can_render_live()``
-gated, so it can never spend a video-second the gate would refuse.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field

from app.scheduler.adaptive import AdaptiveConfig, Watermarks, adapt_watermarks
from app.scheduler.prediction import ReadingModel
from app.scheduler.zones import (
    DEFAULT_VELOCITY_WPS,
    VELOCITY_CLAMP_HIGH,
    eta_seconds,
)

#: A focus jump larger than this many words (and not explained by the current
#: velocity over the elapsed dt) is treated as a §4.8 seek, not steady reading.
DEFAULT_JUMP_WORDS = 250
#: Window (in samples) over which the directional sign of motion is summarised
#: for the re-read detector — short enough to react, long enough to ignore a
#: single backward glance.
DEFAULT_DIRECTION_WINDOW = 6
#: Multiplier of the §4.3 clamp ceiling above which the *raw* velocity reads as a
#: skim (mirrors the §4.6 gate; configurable so a noisy book can be tuned).
DEFAULT_SKIM_CEILING_MULTIPLE = 1.0
#: Fraction of recent motion that must be backward for a REREADING verdict.
DEFAULT_REREAD_BACKWARD_FRACTION = 0.35
#: Dwell (ms) above which a slow-but-forward reader reads as PONDERING.
DEFAULT_PONDER_DWELL_MS = 6_000.0
#: Samples the classifier needs before leaving the cold-start STEADY default.
DEFAULT_REGIME_MIN_SAMPLES = 3
#: How many ticks a JUMPING verdict persists after the last detected jump, so the
#: band stays collapsed until the trajectory has a chance to re-stabilise.
DEFAULT_JUMP_HOLD_TICKS = 2


class ReaderRegime(StrEnum):
    """The named reading behaviours the adaptive layer schedules against (§4.6)."""

    STEADY = "steady"
    SKIMMING = "skimming"
    REREADING = "rereading"
    PONDERING = "pondering"
    JUMPING = "jumping"


@dataclass(frozen=True, slots=True)
class RegimeConfig:
    """Tunable thresholds for the regime classifier (default to the constants)."""

    jump_words: int = DEFAULT_JUMP_WORDS
    direction_window: int = DEFAULT_DIRECTION_WINDOW
    skim_ceiling_multiple: float = DEFAULT_SKIM_CEILING_MULTIPLE
    reread_backward_fraction: float = DEFAULT_REREAD_BACKWARD_FRACTION
    ponder_dwell_ms: float = DEFAULT_PONDER_DWELL_MS
    min_samples: int = DEFAULT_REGIME_MIN_SAMPLES
    jump_hold_ticks: int = DEFAULT_JUMP_HOLD_TICKS


@dataclass(frozen=True, slots=True)
class RegimeVerdict:
    """The classifier output: the named regime + the evidence behind it.

    ``confidence`` rises with sample count and the clarity of the signal (a clear
    skim/jump is high-confidence; a borderline steady/ponder is lower). Watermark
    sizing scales its adjustment by confidence so a low-confidence verdict barely
    perturbs the §4.5 baseline.
    """

    regime: ReaderRegime
    mean_wps: float
    raw_mean_wps: float
    backward_fraction: float
    dwell_ms: float
    samples: int
    confidence: float = 1.0


class VelocityRegimeModel(BaseModel):
    """A regime-aware wrapper over the base :class:`ReadingModel` (§4.6).

    Holds the scalar :class:`ReadingModel` plus the small directional/jump state
    needed to name a regime. Fed one :meth:`observe` per settled intent update —
    the same cadence as the base model — and queried with :meth:`classify`. Pure
    and JSON-serialisable so it round-trips Redis next to the session state.
    """

    base: ReadingModel = Field(default_factory=ReadingModel.with_halflives)
    #: Recent motion signs (+1 forward, -1 backward) over the direction window.
    recent_directions: list[int] = Field(default_factory=list)
    #: Ticks since the last detected jump (>= jump_hold_ticks ⇒ no longer JUMPING).
    ticks_since_jump: int = 1_000_000
    #: Total samples folded in (mirrors base.samples; kept for the verdict).
    samples: int = 0

    model_config = {"arbitrary_types_allowed": True}

    @classmethod
    def fresh(cls, *, velocity_wps: float = DEFAULT_VELOCITY_WPS) -> VelocityRegimeModel:
        """A cold model seeded at ``velocity_wps`` (cold-start ⇒ STEADY)."""
        return cls(base=ReadingModel.with_halflives(velocity_wps=velocity_wps))

    # -- online update ------------------------------------------------------- #

    def observe(
        self,
        *,
        words_advanced: int,
        dt_ms: float,
        config: RegimeConfig | None = None,
    ) -> None:
        """Fold one settled update into the regime model (§4.3/§4.8).

        Delegates the velocity/variance/dwell math to the base model, then updates
        the directional window and jump state this layer adds. A large
        *unexplained* focus delta (more than ``jump_words`` beyond what the current
        velocity could cover in ``dt_ms``) is recorded as a jump (§4.8 seek); the
        base model is *not* fed the jump as a velocity sample, because a teleport
        carries no reading-rate information and would corrupt ``v``.
        """
        config = config or RegimeConfig()
        if dt_ms <= 0.0:
            return

        # Jump detection: a focus delta far beyond plausible reading over dt.
        plausible = self.base.predict_velocity().mean_wps * (dt_ms / 1000.0)
        is_jump = abs(words_advanced) > max(config.jump_words, plausible + config.jump_words)

        self.ticks_since_jump += 1
        if is_jump:
            self.ticks_since_jump = 0
            # A seek is not a reading-velocity sample; only count it as a sample so
            # confidence still grows, and record direction for the re-read signal.
            self.samples += 1
            self._push_direction(1 if words_advanced >= 0 else -1, config)
            return

        self.base.observe(words_advanced=words_advanced, dt_ms=dt_ms)
        self.samples += 1
        if words_advanced != 0:
            self._push_direction(1 if words_advanced > 0 else -1, config)

    def _push_direction(self, sign: int, config: RegimeConfig) -> None:
        window = deque(self.recent_directions, maxlen=max(1, config.direction_window))
        window.append(sign)
        self.recent_directions = list(window)

    # -- classification ------------------------------------------------------ #

    def backward_fraction(self) -> float:
        """Fraction of recent motion samples that were backward (re-read signal)."""
        if not self.recent_directions:
            return 0.0
        backward = sum(1 for d in self.recent_directions if d < 0)
        return backward / len(self.recent_directions)

    def classify(self, *, config: RegimeConfig | None = None) -> RegimeVerdict:
        """Name the current reading regime (§4.6).

        Decision order (first match wins), chosen so the *cheapest-to-be-wrong*
        verdicts dominate: a recent jump collapses everything (JUMPING); a velocity
        over the clamp ceiling is a skim regardless of direction (SKIMMING); a
        backward-dominated window is a re-read (REREADING); a slow reader with long
        dwell is a thinker (PONDERING); otherwise STEADY.

        Cold-start (``samples < min_samples``) is always STEADY with low
        confidence, so a fresh session behaves like the un-modelled §4.5 default.
        """
        config = config or RegimeConfig()
        pred = self.base.predict_velocity()
        dwell = self.base.predict_dwell_ms()
        bwd = self.backward_fraction()

        def verdict(regime: ReaderRegime, confidence: float) -> RegimeVerdict:
            return RegimeVerdict(
                regime=regime,
                mean_wps=pred.mean_wps,
                raw_mean_wps=pred.raw_mean_wps,
                backward_fraction=bwd,
                dwell_ms=dwell,
                samples=self.samples,
                confidence=max(0.0, min(1.0, confidence)),
            )

        if self.samples < config.min_samples:
            return verdict(ReaderRegime.STEADY, 0.2)

        # Confidence floor that grows with sample count (caps at 1.0 by ~12).
        base_conf = min(1.0, self.samples / 12.0)

        if self.ticks_since_jump < config.jump_hold_ticks:
            return verdict(ReaderRegime.JUMPING, 1.0)

        skim_ceiling = VELOCITY_CLAMP_HIGH * config.skim_ceiling_multiple
        if pred.raw_mean_wps > skim_ceiling:
            # The further over the ceiling, the more certain the skim.
            over = (pred.raw_mean_wps - skim_ceiling) / max(skim_ceiling, 1.0)
            return verdict(ReaderRegime.SKIMMING, min(1.0, 0.6 + over))

        if bwd >= config.reread_backward_fraction:
            return verdict(ReaderRegime.REREADING, min(1.0, base_conf * (0.5 + bwd)))

        is_slow = pred.mean_wps <= DEFAULT_VELOCITY_WPS
        if is_slow and dwell >= config.ponder_dwell_ms:
            return verdict(ReaderRegime.PONDERING, base_conf)

        return verdict(ReaderRegime.STEADY, base_conf)


@dataclass(frozen=True, slots=True)
class PageNeed:
    """A predicted upcoming shot that will need rendering inside the horizon.

    ``eta_s`` is the §4.3 reading-time to the shot at the predicted velocity;
    ``urgency`` is ``1.0`` at the playhead decaying with ETA (the order the fill
    loop should consider promotions in).
    """

    shot_id: str
    word_index_start: int
    est_duration_s: float
    eta_s: float
    urgency: float


@dataclass(frozen=True, slots=True)
class UpcomingShot:
    """A thin, infra-free view of an upcoming shot for page-need prediction."""

    shot_id: str
    word_index_start: int
    est_duration_s: float


def predict_pages_needed(
    upcoming: list[UpcomingShot],
    *,
    focus_word: int,
    verdict: RegimeVerdict,
    commit_horizon_s: float,
) -> list[PageNeed]:
    """Predict which upcoming shots will need rendering before the reader arrives.

    Forward-only (§4.6 forecasts the reader's *expected* drift forward): a shot is
    "needed" when its ETA at the predicted velocity is inside the commit horizon.
    SKIMMING / JUMPING regimes return ``[]`` — the §4.6 gate suspends promotion, so
    there is nothing to pre-render. REREADING also returns ``[]`` for *forward*
    shots (the reader is going backward into cached content). The result is sorted
    nearest-ETA first so the caller promotes the most urgent shots within budget.
    """
    if verdict.regime in (ReaderRegime.SKIMMING, ReaderRegime.JUMPING, ReaderRegime.REREADING):
        return []

    v = max(0.1, verdict.mean_wps)
    horizon = max(1.0, commit_horizon_s)
    needs: list[PageNeed] = []
    for shot in upcoming:
        if shot.word_index_start <= focus_word:
            continue
        eta = eta_seconds(shot.word_index_start, focus_word, v)
        if eta > commit_horizon_s:
            continue
        # 1.0 at the playhead, decaying to ~0.37 at the horizon (matches optimizer).
        urgency = _exp_decay(eta, horizon)
        needs.append(
            PageNeed(
                shot_id=shot.shot_id,
                word_index_start=shot.word_index_start,
                est_duration_s=shot.est_duration_s,
                eta_s=eta,
                urgency=urgency,
            )
        )
    needs.sort(key=lambda n: n.eta_s)
    return needs


def size_watermarks(
    base: Watermarks,
    model: VelocityRegimeModel,
    *,
    regime_config: RegimeConfig | None = None,
    adaptive_config: AdaptiveConfig | None = None,
) -> tuple[Watermarks, RegimeVerdict]:
    """Regime-aware watermark sizing, layered on the §4.5 variance widening.

    Two-stage, both bounded and safe:

    1. **Variance widening** — defer to the existing
       :func:`app.scheduler.adaptive.adapt_watermarks`, which deepens the band for
       a noisy reader and never shrinks below ``base`` or breaks ``L < C < H``.
    2. **Regime shaping** — scale the *widened* band by a per-regime factor:
       STEADY/PONDERING keep or modestly deepen it (a thinker rides out long
       dwell); SKIMMING/REREADING/JUMPING *shrink* it toward ``base`` (don't
       pre-spend on pages the reader won't linearly consume). The shrink is scaled
       by the verdict confidence and floored at ``base`` so a thin/low-confidence
       verdict barely moves the baseline, and the ``L < C < H`` band + ordering
       invariant is always re-enforced.

    Returns ``(sized, verdict)``. The shrink direction means this layer can only
    *reduce* speculative spend versus the variance-widened band; PONDERING's modest
    deepening still promotes under the unchanged ``can_render_live()`` gate.
    """
    rc = regime_config or RegimeConfig()
    verdict = model.classify(config=rc)
    widened = adapt_watermarks(base, model.base, config=adaptive_config)

    # Per-regime target multiple of the *widened-over-base* extra band. ``1.0``
    # keeps the full §4.5 variance/velocity widening; ``<1.0`` pulls back toward
    # base (less speculative spend); ``>1.0`` deepens. A STEADY reader keeps only a
    # *fraction* of the widening — the §4.5 rule widens H with raw velocity, which
    # over-provisions a fast-but-metronomic reader who is in no danger of a stall,
    # so we trim it back (the variance component still survives the fraction). A
    # PONDERING reader (long dwell, slow) keeps a deeper band to ride out the next
    # think without draining. SKIMMING / JUMPING collapse to base (don't pre-spend
    # on pages a skimmer/seeker won't linearly consume); REREADING halves it
    # (backward reads replay from cache — §4.8 — so the forward band can shrink).
    target = {
        ReaderRegime.STEADY: 0.4,
        ReaderRegime.PONDERING: 1.15,
        ReaderRegime.REREADING: 0.5,
        ReaderRegime.SKIMMING: 0.0,
        ReaderRegime.JUMPING: 0.0,
    }[verdict.regime]

    # Blend toward the target by confidence (low confidence ⇒ stay at widened).
    factor = 1.0 + (target - 1.0) * verdict.confidence

    def shape(widened_v: float, base_v: float) -> float:
        # Scale the extra band (widened - base) by factor; never below base.
        extra = max(0.0, widened_v - base_v) * max(0.0, factor)
        # PONDERING may deepen past the widened band, but bound it at 1.25× base.
        return max(base_v, min(base_v + extra, base_v * 1.25 + extra))

    low = shape(widened.low_s, base.low_s)
    high = shape(widened.high_s, base.high_s)
    commit = shape(widened.commit_horizon_s, base.commit_horizon_s)

    # Re-enforce the band + ordering invariant (mirror adapt_watermarks' floor).
    min_band = (adaptive_config or AdaptiveConfig()).min_band_s
    high = max(high, low + min_band)
    commit = max(low + 1.0, min(commit, high - 1.0))
    return (
        Watermarks(
            low_s=round(low, 4),
            high_s=round(high, 4),
            commit_horizon_s=round(commit, 4),
        ),
        verdict,
    )


def _exp_decay(value: float, scale: float) -> float:
    """``exp(-value/scale)`` in ``(0, 1]`` — urgency at the playhead vs the horizon."""
    return math.exp(-max(0.0, value) / max(1.0, scale))


__all__ = [
    "DEFAULT_DIRECTION_WINDOW",
    "DEFAULT_JUMP_HOLD_TICKS",
    "DEFAULT_JUMP_WORDS",
    "DEFAULT_PONDER_DWELL_MS",
    "DEFAULT_REGIME_MIN_SAMPLES",
    "DEFAULT_REREAD_BACKWARD_FRACTION",
    "DEFAULT_SKIM_CEILING_MULTIPLE",
    "PageNeed",
    "ReaderRegime",
    "RegimeConfig",
    "RegimeVerdict",
    "UpcomingShot",
    "VelocityRegimeModel",
    "predict_pages_needed",
    "size_watermarks",
]
