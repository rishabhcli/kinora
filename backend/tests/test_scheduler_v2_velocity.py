"""Reader-velocity *regime* model + page-need + watermark-sizing tests (§4.6).

Pure, no infra. Pins :class:`app.scheduler.v2.velocity.VelocityRegimeModel`'s
classifier across the five regimes, the forward-only page-need prediction, and the
regime-aware watermark sizer — proving it composes with (never breaks) the existing
§4.5 variance widening and only ever *shrinks* the band for non-linear regimes.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.scheduler.adaptive import adapt_watermarks, base_watermarks
from app.scheduler.v2.velocity import (
    PageNeed,
    ReaderRegime,
    RegimeConfig,
    UpcomingShot,
    VelocityRegimeModel,
    predict_pages_needed,
    size_watermarks,
)

_SETTINGS = get_settings()  # L=25, H=75, C=45


def _feed(model: VelocityRegimeModel, samples: list[tuple[int, float]]) -> VelocityRegimeModel:
    for words, dt_ms in samples:
        model.observe(words_advanced=words, dt_ms=dt_ms)
    return model


def _steady(wps: float, n: int = 12) -> VelocityRegimeModel:
    m = VelocityRegimeModel.fresh(velocity_wps=wps)
    # ~wps words per second; 1000ms gaps so words==wps each sample (metronomic).
    return _feed(m, [(int(round(wps)), 1000.0)] * n)


# --- cold-start --------------------------------------------------------------- #


def test_cold_start_is_steady_low_confidence() -> None:
    m = VelocityRegimeModel.fresh()
    v = m.classify()
    assert v.regime is ReaderRegime.STEADY
    assert v.confidence < 0.5  # not trusted yet
    assert v.samples == 0


def test_below_min_samples_stays_steady() -> None:
    m = VelocityRegimeModel.fresh()
    m.observe(words_advanced=40, dt_ms=1000.0)  # one big forward sample
    # 1 sample < min_samples(3) ⇒ still STEADY, regardless of velocity.
    assert m.classify().regime is ReaderRegime.STEADY


# --- the five regimes --------------------------------------------------------- #


def test_metronomic_reader_classifies_steady() -> None:
    m = _steady(4.0)
    v = m.classify()
    assert v.regime is ReaderRegime.STEADY
    assert v.backward_fraction == 0.0


def test_fast_above_clamp_ceiling_classifies_skimming() -> None:
    # 16 wps is well above the 12 wps clamp ceiling.
    m = _steady(16.0)
    assert m.classify().regime is ReaderRegime.SKIMMING


def test_backward_dominated_window_classifies_rereading() -> None:
    m = VelocityRegimeModel.fresh(velocity_wps=4.0)
    # A run of backward reads (re-reading a passage).
    _feed(m, [(-4, 1000.0)] * 6)
    v = m.classify()
    assert v.regime is ReaderRegime.REREADING
    assert v.backward_fraction >= 0.35


def test_slow_long_dwell_classifies_pondering() -> None:
    m = VelocityRegimeModel.fresh(velocity_wps=2.0)
    # Slow forward reading with long inter-update gaps (thinker between thinks).
    # Enough samples for the dwell EWMA to climb past the 200ms cold-start default.
    _feed(m, [(4, 9000.0)] * 12)
    v = m.classify()
    assert v.regime is ReaderRegime.PONDERING
    assert v.dwell_ms >= _SETTINGS_PONDER_FLOOR


def test_large_unexplained_jump_classifies_jumping() -> None:
    m = _steady(4.0)
    # A teleport far beyond plausible reading in the gap = a §4.8 seek.
    m.observe(words_advanced=5000, dt_ms=1000.0)
    assert m.classify().regime is ReaderRegime.JUMPING


def test_jumping_verdict_decays_after_hold_ticks() -> None:
    m = _steady(4.0)
    m.observe(words_advanced=5000, dt_ms=1000.0)
    assert m.classify().regime is ReaderRegime.JUMPING
    # After jump_hold_ticks of normal reading, JUMPING clears.
    _feed(m, [(4, 1000.0)] * 3)
    assert m.classify().regime is not ReaderRegime.JUMPING


# --- jump is not a velocity sample ------------------------------------------- #


def test_jump_does_not_corrupt_velocity_estimate() -> None:
    m = _steady(4.0)
    before = m.base.predict_velocity().mean_wps
    m.observe(words_advanced=8000, dt_ms=1000.0)  # a teleport
    after = m.base.predict_velocity().mean_wps
    # The teleport must not be folded into v (it carries no reading-rate info).
    assert abs(after - before) < 1e-6


# --- page-need prediction ----------------------------------------------------- #


def _shots(n: int, spacing: int = 30) -> list[UpcomingShot]:
    return [
        UpcomingShot(shot_id=f"s{i}", word_index_start=i * spacing, est_duration_s=5.0)
        for i in range(1, n + 1)
    ]


def test_steady_reader_needs_pages_inside_horizon_sorted_by_eta() -> None:
    m = _steady(4.0)
    needs = predict_pages_needed(
        _shots(40), focus_word=0, verdict=m.classify(), commit_horizon_s=45.0
    )
    assert needs, "a steady reader needs upcoming pages"
    assert all(isinstance(n, PageNeed) for n in needs)
    # Sorted nearest-ETA first; all inside the commit horizon.
    etas = [n.eta_s for n in needs]
    assert etas == sorted(etas)
    assert max(etas) <= 45.0
    # Urgency is monotone-decreasing in ETA.
    urg = [n.urgency for n in needs]
    assert urg == sorted(urg, reverse=True)


def test_skimmer_needs_no_pages() -> None:
    m = _steady(16.0)
    needs = predict_pages_needed(
        _shots(40), focus_word=0, verdict=m.classify(), commit_horizon_s=45.0
    )
    assert needs == []  # §4.6 suspends promotion for a skimmer


def test_rereader_needs_no_forward_pages() -> None:
    m = VelocityRegimeModel.fresh(velocity_wps=4.0)
    _feed(m, [(-4, 1000.0)] * 6)
    needs = predict_pages_needed(
        _shots(40), focus_word=600, verdict=m.classify(), commit_horizon_s=45.0
    )
    assert needs == []  # backward reader rides cached content, no forward render


def test_jumping_needs_no_pages() -> None:
    m = _steady(4.0)
    m.observe(words_advanced=5000, dt_ms=1000.0)
    needs = predict_pages_needed(
        _shots(40), focus_word=5000, verdict=m.classify(), commit_horizon_s=45.0
    )
    assert needs == []


# --- watermark sizing --------------------------------------------------------- #


def test_steady_sizing_stays_near_base_no_overprovision() -> None:
    base = base_watermarks(_SETTINGS)
    m = _steady(8.0)  # fast but metronomic
    sized, verdict = size_watermarks(base, m)
    assert verdict.regime is ReaderRegime.STEADY
    # A fast steady reader should NOT get the full velocity widening (that would
    # over-provision a reader in no stall danger) — sized H stays well under 2×.
    assert base.high_s <= sized.high_s <= base.high_s * 1.5
    _assert_band_invariant(sized)


def test_skimmer_sizing_collapses_toward_base() -> None:
    base = base_watermarks(_SETTINGS)
    m = _steady(16.0)
    widened = adapt_watermarks(base, m.base)
    sized, verdict = size_watermarks(base, m)
    assert verdict.regime is ReaderRegime.SKIMMING
    # SKIMMING collapses the band back *toward* base (don't pre-spend on a
    # skimmer): the sized H is far closer to base than to the velocity-widened H.
    assert sized.low_s == base.low_s
    assert sized.commit_horizon_s == base.commit_horizon_s
    assert sized.high_s < base.high_s + 0.15 * (widened.high_s - base.high_s)
    _assert_band_invariant(sized)


def test_pondering_sizing_deepens_band() -> None:
    base = base_watermarks(_SETTINGS)
    m = VelocityRegimeModel.fresh(velocity_wps=2.0)
    _feed(m, [(4, 8000.0)] * 10)
    sized, verdict = size_watermarks(base, m)
    assert verdict.regime is ReaderRegime.PONDERING
    # A thinker gets a deeper buffer to ride out the next think.
    assert sized.high_s >= base.high_s
    _assert_band_invariant(sized)


def test_sizing_always_preserves_band_invariant() -> None:
    base = base_watermarks(_SETTINGS)
    for wps in (1.0, 4.0, 8.0, 16.0):
        sized, _ = size_watermarks(base, _steady(wps))
        _assert_band_invariant(sized)
        # Never below base on any watermark.
        assert sized.low_s >= base.low_s
        assert sized.high_s >= base.high_s
        assert sized.commit_horizon_s >= base.commit_horizon_s


def test_sizing_is_deterministic() -> None:
    base = base_watermarks(_SETTINGS)
    a, _ = size_watermarks(base, _steady(6.0))
    b, _ = size_watermarks(base, _steady(6.0))
    assert a.as_tuple() == b.as_tuple()


# --- helpers ------------------------------------------------------------------ #

_SETTINGS_PONDER_FLOOR = RegimeConfig().ponder_dwell_ms


def _assert_band_invariant(wm) -> None:  # type: ignore[no-untyped-def]
    assert 0 < wm.low_s < wm.commit_horizon_s < wm.high_s
