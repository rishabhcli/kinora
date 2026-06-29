"""Property tests for the §4.3/§4.6 reading-behaviour model + buffer math.

Two pure surfaces:

* :class:`ReadingModel` — the online EWMA velocity/variance/dwell estimator. Its
  invariants (clamped velocity, non-negative variance/dwell, EWMA bounded between
  prev and sample, monotone forecast) keep the predictions the adaptive scheduler
  reads physically meaningful.
* :meth:`SchedulerSession.recompute_committed_ahead` — the §4.5/§4.10 buffer
  measure. Its invariants (non-negative, only-ahead shots count, monotone-drains
  as the reader advances — the sawtooth) are what make the watermark hysteresis
  well-defined.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.scheduler.model import BufferedShot, SchedulerSession
from app.scheduler.prediction import ReadingModel
from app.scheduler.zones import VELOCITY_CLAMP_HIGH, VELOCITY_CLAMP_LOW

# --------------------------------------------------------------------------- #
# ReadingModel — the EWMA estimator
# --------------------------------------------------------------------------- #

#: A single (words_advanced, dt_ms) observation.
#:
#: ``dt_ms`` is floored at 1e-3 (one microsecond): the §4.7 settle cadence is
#: ~200ms, so a realistic inter-update gap is never sub-microsecond. The interval
#: ``(0, 1e-3)`` is *deliberately excluded* here — it triggers a real
#: division-by-zero bug (BUG-1 in DESIGN.md), pinned separately by
#: ``test_subnormal_dt_ms_crashes_observe_BUG1`` so this property suite stays green
#: while the defect is tracked.
observations = st.tuples(
    st.integers(min_value=0, max_value=2000),
    st.one_of(
        st.just(0.0),  # the documented duplicate-timestamp case (ignored)
        st.floats(min_value=1e-3, max_value=10_000.0, allow_nan=False),
    ),
)
observation_runs = st.lists(observations, min_size=0, max_size=40)


def _feed(obs: list[tuple[int, float]]) -> ReadingModel:
    model = ReadingModel()
    for words, dt in obs:
        model.observe(words_advanced=words, dt_ms=dt)
    return model


@given(observation_runs)
def test_predicted_velocity_is_clamped_and_finite(obs: list[tuple[int, float]]) -> None:
    """The reported (clamped) velocity always lands in the §4.3 band, finite."""
    pred = _feed(obs).predict_velocity()
    assert VELOCITY_CLAMP_LOW <= pred.mean_wps <= VELOCITY_CLAMP_HIGH
    assert math.isfinite(pred.mean_wps)
    assert math.isfinite(pred.raw_mean_wps)
    assert pred.raw_mean_wps >= 0.0


@given(observation_runs)
def test_variance_and_std_are_nonnegative(obs: list[tuple[int, float]]) -> None:
    """EWMA variance can't go negative, so std is real and ≥ 0."""
    pred = _feed(obs).predict_velocity()
    assert pred.std_wps >= 0.0
    assert math.isfinite(pred.std_wps)


@given(observation_runs)
def test_dwell_is_nonnegative(obs: list[tuple[int, float]]) -> None:
    assert _feed(obs).predict_dwell_ms() >= 0.0


@given(observation_runs)
def test_coefficient_of_variation_is_nonnegative_and_finite(
    obs: list[tuple[int, float]],
) -> None:
    cv = _feed(obs).predict_velocity().coefficient_of_variation
    assert cv >= 0.0
    assert math.isfinite(cv)


@given(observation_runs)
def test_sample_count_tracks_valid_observations(obs: list[tuple[int, float]]) -> None:
    """Each ``dt_ms > 0`` observation advances the sample count exactly once."""
    valid = sum(1 for _w, dt in obs if dt > 0)
    assert _feed(obs).samples == valid


@given(observation_runs)
def test_zero_dt_observations_are_ignored(obs: list[tuple[int, float]]) -> None:
    """A duplicate-timestamp sample (dt ≤ 0) never perturbs the model."""
    base = _feed(obs)
    perturbed = _feed(obs)
    perturbed.observe(words_advanced=999, dt_ms=0.0)
    assert perturbed.model_dump() == base.model_dump()


@given(
    st.floats(min_value=0.5, max_value=20.0, allow_nan=False),
    st.floats(min_value=0.5, max_value=20.0, allow_nan=False),
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
def test_ewma_is_between_prev_and_sample(prev: float, sample: float, alpha: float) -> None:
    """The EWMA update is a convex combination — bounded by its two inputs."""
    out = ReadingModel._ewma(prev, sample, alpha)
    assert min(prev, sample) - 1e-9 <= out <= max(prev, sample) + 1e-9


@given(
    st.integers(min_value=0, max_value=1_000_000),
    st.floats(min_value=0.0, max_value=600.0, allow_nan=False),
    observation_runs,
)
def test_forecast_is_monotone_and_never_goes_backward(
    focus: int, horizon: float, obs: list[tuple[int, float]]
) -> None:
    """A forward forecast never lands behind the current focus, and grows with horizon."""
    model = _feed(obs)
    here = model.forecast_focus_word(focus, 0.0)
    ahead = model.forecast_focus_word(focus, horizon)
    farther = model.forecast_focus_word(focus, horizon + 60.0)
    assert here == focus
    assert ahead >= focus
    assert farther >= ahead


@given(observation_runs)
def test_cold_start_is_steady(obs: list[tuple[int, float]]) -> None:
    """A model with <2 samples reports steady (cold-start ⇒ no regression, §4.6)."""
    model = _feed(obs)
    if model.samples < 2:
        assert model.is_steady()


@pytest.mark.xfail(
    reason="BUG-1 (DESIGN.md): subnormal dt_ms underflows to 0 after /1000 → ZeroDivisionError",
    raises=ZeroDivisionError,
    strict=True,
)
def test_subnormal_dt_ms_crashes_observe_BUG1() -> None:
    """Regression pin for BUG-1: a positive-subnormal ``dt_ms`` crashes ``observe``.

    ``dt_ms = 5e-324`` (the smallest positive double) is > 0 and clears the
    ``dt_ms <= 0.0`` guard, but ``dt_ms / 1000.0`` underflows to exactly 0.0, so
    ``abs(words) / 0.0`` raises. Reachable from the live IntentController when two
    settled intents land with a vanishing positive clock delta. Marked
    ``xfail(strict)`` so it flips to a failure the moment the divide is guarded
    (the fix is spawned as a separate task); until then it documents the defect
    without reddening the suite.
    """
    ReadingModel().observe(words_advanced=1, dt_ms=5e-324)


# --------------------------------------------------------------------------- #
# SchedulerSession.recompute_committed_ahead — the §4.5/§4.10 buffer measure
# --------------------------------------------------------------------------- #

buffered_shots = st.builds(
    BufferedShot,
    shot_id=st.text(min_size=1, max_size=6),
    word_index_start=st.integers(min_value=0, max_value=100_000),
    est_duration_s=st.floats(min_value=0.0, max_value=15.0, allow_nan=False),
    state=st.sampled_from(["inflight", "ready"]),
)


@st.composite
def sessions_with_buffer(draw: st.DrawFn) -> SchedulerSession:
    shots = draw(st.lists(buffered_shots, max_size=20, unique_by=lambda s: s.shot_id))
    return SchedulerSession(
        session_id="s",
        book_id="b",
        focus_word=draw(st.integers(min_value=0, max_value=100_000)),
        committed_buffer=shots,
    )


@given(sessions_with_buffer())
def test_committed_ahead_is_nonnegative(session: SchedulerSession) -> None:
    assert session.recompute_committed_ahead() >= 0.0


@given(sessions_with_buffer())
def test_committed_ahead_counts_only_shots_ahead(session: SchedulerSession) -> None:
    """Only shots whose start is strictly ahead of ``w`` contribute (§4.5)."""
    w = session.focus_word
    expected = round(
        sum(s.est_duration_s for s in session.committed_buffer if s.word_index_start > w),
        6,
    )
    assert session.recompute_committed_ahead() == expected
    # Surviving buffer entries are exactly the ahead-of-w shots.
    assert all(s.word_index_start > w for s in session.committed_buffer)


@given(sessions_with_buffer(), st.integers(min_value=0, max_value=200_000))
def test_advancing_the_reader_never_increases_the_buffer(
    session: SchedulerSession, advance: int
) -> None:
    """Metamorphic (§4.10): moving the focus *forward* can only drain the buffer.

    This monotone-drain is the falling edge of the watermark sawtooth — a forward
    reader never gains committed-seconds without a new fill.
    """
    before = session.recompute_committed_ahead()
    session.focus_word += advance
    after = session.recompute_committed_ahead()
    assert after <= before + 1e-6


@given(sessions_with_buffer())
def test_recompute_is_idempotent(session: SchedulerSession) -> None:
    """Recomputing twice at the same focus word yields the same buffer + total."""
    first = session.recompute_committed_ahead()
    buffer_after = list(session.committed_buffer)
    second = session.recompute_committed_ahead()
    assert first == second
    assert session.committed_buffer == buffer_after
