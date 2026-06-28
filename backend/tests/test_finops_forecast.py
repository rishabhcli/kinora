"""Reading-trajectory cost forecasting (kinora.md §11.1, §4.6). Pure — no infra."""

from __future__ import annotations

import math

import pytest

from app.finops.forecast import (
    ReadingTrajectory,
    VelocityEstimator,
    build_forecast,
    burn_down,
    forecast_video_seconds,
    seconds_to_exhaustion,
)


def _traj(**kw: object) -> ReadingTrajectory:
    base: dict[str, object] = {
        "velocity_wps": 4.0,
        "words_remaining": 4000,
        "shot_seconds_per_word": 0.02,
        "promotion_rate": 1.0,
        "regen_overhead": 0.0,
    }
    base.update(kw)
    return ReadingTrajectory(**base)  # type: ignore[arg-type]


def test_reading_seconds_remaining() -> None:
    assert _traj(velocity_wps=4.0, words_remaining=4000).reading_seconds_remaining == 1000.0
    assert math.isinf(_traj(velocity_wps=0.0).reading_seconds_remaining)


def test_burn_rate_scales_with_velocity_promotion_and_overhead() -> None:
    # 4 wps * 0.02 s/word = 0.08 video-s per reading-s, full promotion, no regen.
    assert _traj().video_seconds_per_reading_second == pytest.approx(0.08)
    # Half the shots promoted -> half the burn.
    assert _traj(promotion_rate=0.5).video_seconds_per_reading_second == pytest.approx(0.04)
    # 20% regen overhead inflates it.
    assert _traj(regen_overhead=0.2).video_seconds_per_reading_second == pytest.approx(0.096)


def test_forecast_bounded_by_reading_remaining() -> None:
    traj = _traj(velocity_wps=4.0, words_remaining=400)  # only 100s of reading left
    # Horizon 1000s, but reading runs out at 100s -> 0.08 * 100 = 8.0.
    assert forecast_video_seconds(traj, horizon_s=1000.0) == pytest.approx(8.0)
    # Short horizon dominates: 0.08 * 50 = 4.0.
    assert forecast_video_seconds(traj, horizon_s=50.0) == pytest.approx(4.0)


def test_idle_reader_forecasts_zero() -> None:
    assert forecast_video_seconds(_traj(velocity_wps=0.0), horizon_s=1000.0) == 0.0
    assert forecast_video_seconds(_traj(), horizon_s=0.0) == 0.0


def test_seconds_to_exhaustion() -> None:
    assert seconds_to_exhaustion(remaining_s=100.0, burn_rate_s_per_s=0.5) == pytest.approx(200.0)
    assert math.isinf(seconds_to_exhaustion(remaining_s=100.0, burn_rate_s_per_s=0.0))
    assert seconds_to_exhaustion(remaining_s=0.0, burn_rate_s_per_s=1.0) == 0.0
    assert seconds_to_exhaustion(remaining_s=-5.0, burn_rate_s_per_s=1.0) == 0.0


def test_burn_down_curve_is_monotone_and_clamped() -> None:
    traj = _traj(velocity_wps=10.0, words_remaining=100_000)  # plenty of reading
    bd = burn_down(traj, remaining_s=100.0, horizon_s=1000.0, steps=10)
    remaining = [s.remaining_s for s in bd.samples]
    # Non-increasing.
    assert all(remaining[i] >= remaining[i + 1] for i in range(len(remaining) - 1))
    # Never negative.
    assert all(r >= 0.0 for r in remaining)
    # Starts at full remaining, ends clamped at 0 (burn rate 0.2 over 1000s = 200 > 100).
    assert remaining[0] == pytest.approx(100.0)
    assert remaining[-1] == 0.0


def test_burn_down_exhaustion_within_horizon() -> None:
    traj = _traj(velocity_wps=10.0, words_remaining=100_000)  # rate 0.2 s/s
    bd = burn_down(traj, remaining_s=100.0, horizon_s=1000.0)
    assert bd.burn_rate_s_per_s == pytest.approx(0.2)
    assert bd.exhaust_at_s == pytest.approx(500.0)
    assert bd.will_exhaust


def test_burn_down_does_not_exhaust_when_reading_ends_first() -> None:
    # Reader finishes the book (200s of reading) before the budget would run out.
    traj = _traj(velocity_wps=10.0, words_remaining=2000)  # 200s reading, rate 0.2
    bd = burn_down(traj, remaining_s=100.0, horizon_s=1000.0)
    # Would exhaust at 500s but reading ends at 200s -> reported as never.
    assert math.isinf(bd.exhaust_at_s)
    assert not bd.will_exhaust


def test_build_forecast_fits_flag() -> None:
    traj = _traj(velocity_wps=4.0, words_remaining=400)  # forecast 8s over the read
    fits = build_forecast(traj, remaining_s=20.0, horizon_s=1000.0)
    assert fits.forecast_video_s == pytest.approx(8.0)
    assert fits.headroom_after_s == pytest.approx(12.0)
    assert fits.fits

    tight = build_forecast(traj, remaining_s=5.0, horizon_s=1000.0)
    assert not tight.fits
    assert tight.headroom_after_s == pytest.approx(-3.0)


def test_forecast_report_as_dict_serializable() -> None:
    report = build_forecast(_traj(), remaining_s=100.0, horizon_s=200.0)
    d = report.as_dict()
    assert set(d) >= {"horizon_s", "forecast_video_s", "remaining_s", "fits", "burn"}
    assert isinstance(d["burn"], dict)


def test_velocity_estimator_smooths_noise() -> None:
    est = VelocityEstimator(alpha=0.5)
    assert est.value == 0.0  # before any sample
    assert est.update(10.0) == 10.0  # first sample seeds it
    # A noisy spike to 0 is dampened (0.5*0 + 0.5*10 = 5).
    assert est.update(0.0) == pytest.approx(5.0)
    assert est.update(10.0) == pytest.approx(7.5)


def test_velocity_estimator_tracks_a_sustained_change() -> None:
    est = VelocityEstimator(alpha=0.5, initial=4.0)
    # Reader speeds up to 20 and holds; the EWMA converges toward 20.
    for _ in range(20):
        est.update(20.0)
    assert est.value == pytest.approx(20.0, abs=0.01)


def test_velocity_estimator_reset() -> None:
    est = VelocityEstimator()
    est.update(8.0)
    est.reset()
    assert est.value == 0.0
    est.reset(4.0)
    assert est.value == 4.0


def test_velocity_estimator_rejects_bad_alpha() -> None:
    with pytest.raises(ValueError):
        VelocityEstimator(alpha=0.0)
    with pytest.raises(ValueError):
        VelocityEstimator(alpha=1.5)


def test_velocity_estimator_clamps_negative_samples() -> None:
    est = VelocityEstimator(alpha=0.5, initial=10.0)
    # A negative sample is clamped to 0 before folding in.
    assert est.update(-5.0) == pytest.approx(5.0)
