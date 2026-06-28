"""Adaptive-watermark tests (kinora.md §4.5/§4.6) — pure, no infra.

Pin :func:`app.scheduler.adaptive.adapt_watermarks`: a steady reader keeps the
§4.5 constants; a noisy reader gets a deeper buffer band; growth is bounded; the
``L < C < H`` invariant always holds; adaptation never shrinks below base.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.scheduler.adaptive import (
    AdaptiveConfig,
    Watermarks,
    adapt_watermarks,
    base_watermarks,
)
from app.scheduler.prediction import ReadingModel

_SETTINGS = get_settings()  # L=25, H=75, C=45


def _model(samples: list[tuple[int, float]]) -> ReadingModel:
    m = ReadingModel.with_halflives()
    for words, dt_ms in samples:
        m.observe(words_advanced=words, dt_ms=dt_ms)
    return m


def _steady(wps: float, n: int = 40) -> ReadingModel:
    return _model([(int(round(wps)), 1000.0)] * n)


def _jittery(n: int = 40) -> ReadingModel:
    return _model([(10 if i % 2 == 0 else 2, 1000.0) for i in range(n)])


def test_base_is_the_section_4_5_constants() -> None:
    base = base_watermarks(_SETTINGS)
    assert base.as_tuple() == (
        _SETTINGS.watermark_low_s,
        _SETTINGS.watermark_high_s,
        _SETTINGS.commit_horizon_s,
    )


def test_cold_model_keeps_base_watermarks() -> None:
    base = base_watermarks(_SETTINGS)
    cold = ReadingModel.with_halflives()
    assert adapt_watermarks(base, cold).as_tuple() == base.as_tuple()


def test_steady_reader_barely_moves_the_watermarks() -> None:
    base = base_watermarks(_SETTINGS)
    tuned = adapt_watermarks(base, _steady(4.0))
    # A metronome at the default velocity → essentially the base band.
    assert abs(tuned.low_s - base.low_s) < 2.0
    assert abs(tuned.high_s - base.high_s) < 4.0


def test_noisy_reader_gets_a_deeper_band() -> None:
    base = base_watermarks(_SETTINGS)
    tuned = adapt_watermarks(base, _jittery())
    assert tuned.low_s > base.low_s  # refill sooner
    assert tuned.high_s > base.high_s  # coast deeper
    assert tuned.high_s - tuned.low_s > 0.0


def test_adaptation_never_shrinks_below_base() -> None:
    base = base_watermarks(_SETTINGS)
    for model in (_steady(2.0), _steady(4.0), _steady(12.0), _jittery()):
        tuned = adapt_watermarks(base, model)
        assert tuned.low_s >= base.low_s
        assert tuned.high_s >= base.high_s
        assert tuned.commit_horizon_s >= base.commit_horizon_s


def test_growth_is_bounded_by_max_multiple() -> None:
    base = base_watermarks(_SETTINGS)
    cfg = AdaptiveConfig(max_multiple=2.0)
    # An extremely jittery reader can't blow the band past 2× base.
    tuned = adapt_watermarks(base, _jittery(80), config=cfg)
    assert tuned.low_s <= base.low_s * 2.0 + 1e-6
    assert tuned.high_s <= base.high_s * 2.0 + 1e-6
    assert tuned.commit_horizon_s <= base.commit_horizon_s * 2.0 + 1e-6


def test_ordering_invariant_always_holds() -> None:
    base = base_watermarks(_SETTINGS)
    for model in (_steady(2.0), _steady(8.0), _steady(20.0), _jittery()):
        t: Watermarks = adapt_watermarks(base, model)
        assert 0 < t.low_s < t.commit_horizon_s < t.high_s
