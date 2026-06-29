"""Unit tests for demand forecasting (app.inference.scaling.forecast)."""

from __future__ import annotations

import pytest

from app.inference.scaling.forecast import (
    EwmaForecaster,
    Forecast,
    HoltForecaster,
    HoltWintersForecaster,
    z_for_quantile,
)

# --------------------------------------------------------------------------- #
# z_for_quantile
# --------------------------------------------------------------------------- #


def test_z_table_values() -> None:
    assert z_for_quantile(0.5) == pytest.approx(0.0)
    assert z_for_quantile(0.95) == pytest.approx(1.6449, abs=1e-3)
    assert z_for_quantile(0.99) == pytest.approx(2.3263, abs=1e-3)


def test_z_symmetry_for_lower_quantiles() -> None:
    assert z_for_quantile(0.05) == pytest.approx(-z_for_quantile(0.95))


def test_z_approximation_off_table() -> None:
    # 0.9772 ~ 2 sigma; Acklam approx should land close.
    assert z_for_quantile(0.9772) == pytest.approx(2.0, abs=2e-3)


@pytest.mark.parametrize("q", [0.0, 1.0, -0.1, 1.5])
def test_z_rejects_out_of_range(q: float) -> None:
    with pytest.raises(ValueError):
        z_for_quantile(q)


# --------------------------------------------------------------------------- #
# EWMA
# --------------------------------------------------------------------------- #


def test_ewma_tracks_a_constant() -> None:
    f = EwmaForecaster(alpha=0.5)
    for _ in range(20):
        f.observe(10.0)
    fc = f.forecast(horizon=5)
    assert fc.point == pytest.approx(10.0)
    assert fc.sigma == pytest.approx(0.0)
    assert fc.is_warm


def test_ewma_first_sample_seeds_level() -> None:
    f = EwmaForecaster(alpha=0.3)
    f.observe(7.0)
    assert f.forecast(1).point == pytest.approx(7.0)


def test_ewma_forecast_is_flat_across_horizon() -> None:
    f = EwmaForecaster(alpha=0.4)
    for v in (5.0, 6.0, 7.0):
        f.observe(v)
    assert f.forecast(1).point == pytest.approx(f.forecast(100).point)


def test_ewma_floors_negative_level_at_zero() -> None:
    f = EwmaForecaster(alpha=1.0)
    f.observe(0.0)
    assert f.forecast(1).point == 0.0


def test_ewma_rejects_bad_alpha() -> None:
    with pytest.raises(ValueError):
        EwmaForecaster(alpha=0.0)
    with pytest.raises(ValueError):
        EwmaForecaster(alpha=1.5)


# --------------------------------------------------------------------------- #
# Holt (trend)
# --------------------------------------------------------------------------- #


def test_holt_projects_a_linear_ramp() -> None:
    f = HoltForecaster(alpha=0.5, beta=0.5)
    # A clean linear ramp: 0, 2, 4, 6, 8, ...
    for v in range(0, 40, 2):
        f.observe(float(v))
    fc = f.forecast(horizon=1)
    # Next value of the ramp is ~the last + 2.
    assert fc.point == pytest.approx(40.0, abs=1.0)
    far = f.forecast(horizon=5)
    assert far.point > fc.point  # trend carried forward


def test_holt_horizon_widens_sigma() -> None:
    f = HoltForecaster(alpha=0.4, beta=0.2)
    for v in (10.0, 12.0, 9.0, 11.0, 10.5, 9.5, 11.5, 10.0):
        f.observe(v)
    near = f.forecast(1)
    far = f.forecast(4)
    assert far.sigma >= near.sigma  # sqrt(horizon) widening


def test_holt_flat_series_has_zero_trend() -> None:
    f = HoltForecaster()
    for _ in range(10):
        f.observe(5.0)
    assert f.trend == pytest.approx(0.0, abs=1e-9)
    assert f.forecast(10).point == pytest.approx(5.0)


def test_holt_rejects_bad_params() -> None:
    with pytest.raises(ValueError):
        HoltForecaster(alpha=0.0)
    with pytest.raises(ValueError):
        HoltForecaster(beta=-0.1)


# --------------------------------------------------------------------------- #
# Holt-Winters (seasonality)
# --------------------------------------------------------------------------- #


def test_holt_winters_learns_a_seasonal_cycle() -> None:
    # A period-4 seasonal pattern repeated many times: peak at slot 1.
    pattern = [10.0, 30.0, 10.0, 5.0]
    f = HoltWintersForecaster(period=4, alpha=0.3, beta=0.0, gamma=0.5)
    for _ in range(20):
        for v in pattern:
            f.observe(v)
    # Forecasting one step ahead of a slot-0 sample should anticipate the slot-1 peak.
    # After 80 samples (multiple of 4) the next slot is 0, so horizon=2 targets slot 1.
    fc_peak = f.forecast(horizon=2)
    fc_trough = f.forecast(horizon=4)  # targets slot 3 (the trough)
    assert fc_peak.point > fc_trough.point


def test_holt_winters_falls_back_before_one_period() -> None:
    f = HoltWintersForecaster(period=10)
    f.observe(5.0)
    f.observe(6.0)
    # Fewer than `period` samples => no seasonal term, behaves Holt-like.
    fc = f.forecast(1)
    assert fc.point >= 0.0


def test_holt_winters_rejects_bad_period() -> None:
    with pytest.raises(ValueError):
        HoltWintersForecaster(period=1)


def test_holt_winters_default_seasonals_sized_to_period() -> None:
    f = HoltWintersForecaster(period=7)
    assert len(f.seasonals) == 7


# --------------------------------------------------------------------------- #
# Forecast quantile + headroom
# --------------------------------------------------------------------------- #


def test_forecast_quantile_adds_headroom() -> None:
    fc = Forecast(point=10.0, sigma=2.0, horizon=1, samples=10)
    p95 = fc.quantile(0.95)
    assert p95 == pytest.approx(10.0 + 1.6449 * 2.0, abs=1e-2)
    assert fc.quantile(0.5) == pytest.approx(10.0)


def test_forecast_quantile_floors_at_zero() -> None:
    fc = Forecast(point=1.0, sigma=5.0, horizon=1, samples=10)
    assert fc.quantile(0.01) == 0.0


def test_forecast_cold_estimator_not_warm() -> None:
    fc = Forecast(point=1.0, sigma=0.0, horizon=1, samples=2)
    assert not fc.is_warm
    assert Forecast(point=1.0, sigma=0.0, horizon=1, samples=3).is_warm
