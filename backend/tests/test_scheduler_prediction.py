"""Reading-behaviour prediction tests (kinora.md §4.3/§4.6) — pure, no infra.

Pin :class:`app.scheduler.prediction.ReadingModel` to known, deterministic values:
the EWMA tracks a step change, variance widens for a jittery reader and stays ~0
for a metronome, dwell follows the inter-update gap, the steadiness gate matches
the §4.6 skim rule, and the forecast is consistent with the clamped ETA velocity.
The model is JSON-serialisable so it round-trips for the Redis persistence path.
"""

from __future__ import annotations

from app.scheduler.prediction import (
    DEFAULT_DWELL_MS,
    ReadingModel,
    VelocityPrediction,
)
from app.scheduler.zones import DEFAULT_VELOCITY_WPS, VELOCITY_CLAMP_HIGH


def _steady_model(wps: float, *, samples: int = 30, dt_ms: float = 1000.0) -> ReadingModel:
    m = ReadingModel.with_halflives()
    words = int(round(wps * dt_ms / 1000.0))
    for _ in range(samples):
        m.observe(words_advanced=words, dt_ms=dt_ms)
    return m


# --- velocity EWMA tracks a constant reader -------------------------------- #


def test_cold_model_is_the_default_velocity() -> None:
    m = ReadingModel.with_halflives()
    pred = m.predict_velocity()
    assert isinstance(pred, VelocityPrediction)
    assert pred.mean_wps == DEFAULT_VELOCITY_WPS
    assert pred.samples == 0


def test_velocity_converges_to_a_constant_reader() -> None:
    m = _steady_model(6.0, samples=40, dt_ms=1000.0)
    pred = m.predict_velocity()
    assert abs(pred.raw_mean_wps - 6.0) < 0.05  # EWMA settled on the true rate
    # Metronome → tiny noise (the small residual is the cold-start step from the
    # 4 wps default decaying out of the EWMA variance).
    assert pred.coefficient_of_variation < 0.05


def test_velocity_is_clamped_for_eta_but_raw_is_exposed() -> None:
    # A skimmer way above the clamp ceiling.
    m = _steady_model(20.0, samples=40, dt_ms=1000.0)
    pred = m.predict_velocity()
    assert pred.mean_wps == VELOCITY_CLAMP_HIGH  # ETA velocity is capped (§4.3)
    assert pred.raw_mean_wps > VELOCITY_CLAMP_HIGH  # but the skim signal survives


# --- variance / steadiness (§4.6) ------------------------------------------ #


def test_metronome_reader_is_steady() -> None:
    m = _steady_model(4.0, samples=40, dt_ms=1000.0)
    assert m.is_steady() is True


def test_jittery_reader_is_not_steady() -> None:
    m = ReadingModel.with_halflives()
    # Alternate fast/slow each second: large velocity variance.
    for i in range(40):
        wps = 10.0 if i % 2 == 0 else 2.0
        m.observe(words_advanced=int(wps), dt_ms=1000.0)
    pred = m.predict_velocity()
    assert pred.coefficient_of_variation > 0.35
    assert m.is_steady() is False


def test_skimmer_above_ceiling_is_not_steady() -> None:
    m = _steady_model(20.0, samples=40, dt_ms=1000.0)
    # Even though it is *consistent*, a reader pinned past the clamp ceiling is
    # unsteady by the §4.6 rule (suspends promotion).
    assert m.is_steady() is False


def test_cold_start_is_treated_as_steady() -> None:
    m = ReadingModel.with_halflives()
    m.observe(words_advanced=4, dt_ms=1000.0)
    assert m.is_steady() is True  # < 2 real samples → no regression vs default


# --- dwell + holds (§4.7) -------------------------------------------------- #


def test_dwell_tracks_the_inter_update_gap() -> None:
    m = ReadingModel.with_halflives()
    assert m.predict_dwell_ms() == DEFAULT_DWELL_MS
    for _ in range(40):
        m.observe(words_advanced=4, dt_ms=2500.0)
    assert abs(m.predict_dwell_ms() - 2500.0) < 50.0


def test_a_hold_informs_dwell_but_not_velocity() -> None:
    m = _steady_model(6.0, samples=20, dt_ms=1000.0)
    before = m.predict_velocity().raw_mean_wps
    # A long thinking pause with no motion: velocity must NOT decay toward 0.
    m.observe(words_advanced=0, dt_ms=8000.0)
    after = m.predict_velocity().raw_mean_wps
    assert abs(after - before) < 1e-9  # velocity untouched by a pure hold
    assert m.predict_dwell_ms() > 1000.0  # dwell rose toward the pause


def test_zero_dt_is_ignored() -> None:
    m = ReadingModel.with_halflives()
    m.observe(words_advanced=100, dt_ms=0.0)  # duplicate timestamp
    assert m.samples == 0  # no division by zero, no sample folded


# --- forecast (§4.6) ------------------------------------------------------- #


def test_forecast_uses_clamped_velocity() -> None:
    m = _steady_model(4.0, samples=40, dt_ms=1000.0)
    # 45s of reading-time at ~4 wps ≈ 180 words ahead of the current focus word.
    forecast = m.forecast_focus_word(1000, 45.0)
    assert 1000 + 160 <= forecast <= 1000 + 200


def test_forecast_is_forward_only() -> None:
    m = ReadingModel.with_halflives()
    # A backward glance (negative words) still leaves the EWMA positive; the
    # forecast never predicts a *backward* expected drift.
    m.observe(words_advanced=-50, dt_ms=1000.0)
    assert m.forecast_focus_word(1000, 30.0) >= 1000


# --- serialisation (Redis persistence path) -------------------------------- #


def test_model_round_trips_through_json() -> None:
    m = _steady_model(5.5, samples=25, dt_ms=1200.0)
    blob = m.model_dump(mode="json")
    restored = ReadingModel.model_validate(blob)
    assert restored.predict_velocity().raw_mean_wps == m.predict_velocity().raw_mean_wps
    assert restored.predict_dwell_ms() == m.predict_dwell_ms()
    assert restored.samples == m.samples
