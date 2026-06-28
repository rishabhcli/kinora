"""Unit tests for workload models + ramp profiles (app.reliability.workload)."""

from __future__ import annotations

import pytest

from app.reliability.workload import (
    ClosedWorkload,
    OpenWorkload,
    RampProfile,
    RampShape,
    ThinkTime,
    WorkloadKind,
    WorkloadPlan,
    windowed_rate,
)

# --------------------------------------------------------------------------- #
# Ramp profiles
# --------------------------------------------------------------------------- #


def test_constant_ramp_is_unity() -> None:
    ramp = RampProfile(shape=RampShape.CONSTANT)
    assert ramp.factor(0.0) == 1.0
    assert ramp.factor(100.0) == 1.0


def test_linear_ramp_warms_from_floor_to_one() -> None:
    ramp = RampProfile(shape=RampShape.LINEAR, ramp_s=10.0, floor=0.2)
    assert ramp.factor(0.0) == pytest.approx(0.2)
    assert ramp.factor(5.0) == pytest.approx(0.6)
    assert ramp.factor(10.0) == pytest.approx(1.0)
    # Past the ramp it holds at 1.0.
    assert ramp.factor(50.0) == pytest.approx(1.0)


def test_step_ramp_jumps_after_ramp_s() -> None:
    ramp = RampProfile(shape=RampShape.STEP, ramp_s=8.0, floor=0.0)
    assert ramp.factor(7.99) == 0.0
    assert ramp.factor(8.0) == 1.0
    assert ramp.factor(20.0) == 1.0


def test_spike_ramp_transient() -> None:
    ramp = RampProfile(
        shape=RampShape.SPIKE, spike_mult=4.0, spike_start_s=10.0, spike_len_s=5.0
    )
    assert ramp.factor(5.0) == 1.0
    assert ramp.factor(12.0) == 4.0
    assert ramp.factor(15.0) == 1.0  # window is [10, 15)


# --------------------------------------------------------------------------- #
# Open model — Poisson arrivals
# --------------------------------------------------------------------------- #


def test_open_arrivals_mean_matches_rate() -> None:
    # Constant 20 rps over 60s -> ~1200 arrivals; average over seeds is close.
    counts = []
    for seed in range(40):
        w = OpenWorkload(base_rate_rps=20.0, seed=seed)
        counts.append(len(w.arrival_times(duration_s=60.0)))
    mean = sum(counts) / len(counts)
    assert mean == pytest.approx(1200, rel=0.05)


def test_open_arrivals_are_sorted_and_in_window() -> None:
    w = OpenWorkload(base_rate_rps=10.0, seed=3)
    times = w.arrival_times(duration_s=30.0)
    assert times == sorted(times)
    assert all(0.0 <= t < 30.0 for t in times)


def test_open_arrivals_deterministic_given_seed() -> None:
    a = OpenWorkload(base_rate_rps=15.0, seed=99).arrival_times(duration_s=20.0)
    b = OpenWorkload(base_rate_rps=15.0, seed=99).arrival_times(duration_s=20.0)
    assert a == b


def test_open_zero_rate_yields_nothing() -> None:
    assert OpenWorkload(base_rate_rps=0.0, seed=1).arrival_times(duration_s=10.0) == []


def test_open_ramp_shapes_intensity() -> None:
    # A linear warm-up means far fewer arrivals in the first half than the second.
    ramp = RampProfile(shape=RampShape.LINEAR, ramp_s=30.0, floor=0.0)
    w = OpenWorkload(base_rate_rps=30.0, ramp=ramp, seed=7)
    times = w.arrival_times(duration_s=30.0)
    first_half = sum(1 for t in times if t < 15.0)
    second_half = sum(1 for t in times if t >= 15.0)
    assert second_half > first_half * 1.5


def test_expected_arrivals_matches_realised_mean() -> None:
    ramp = RampProfile(shape=RampShape.LINEAR, ramp_s=20.0, floor=0.1)
    w = OpenWorkload(base_rate_rps=25.0, ramp=ramp, seed=0)
    analytic = w.expected_arrivals(duration_s=40.0)
    realised = [
        len(OpenWorkload(base_rate_rps=25.0, ramp=ramp, seed=s).arrival_times(duration_s=40.0))
        for s in range(40)
    ]
    mean = sum(realised) / len(realised)
    assert mean == pytest.approx(analytic, rel=0.08)


# --------------------------------------------------------------------------- #
# Closed model — looping users
# --------------------------------------------------------------------------- #


def test_closed_active_users_respects_ramp() -> None:
    cw = ClosedWorkload(
        users=20, ramp=RampProfile(shape=RampShape.LINEAR, ramp_s=10.0, floor=0.0)
    )
    assert cw.active_users(0.0) == 0
    assert cw.active_users(5.0) == 10
    assert cw.active_users(10.0) == 20
    assert cw.active_users(100.0) == 20  # clamped to the cap


def test_closed_active_users_clamped() -> None:
    cw = ClosedWorkload(users=5, ramp=RampProfile(shape=RampShape.SPIKE, spike_mult=10.0))
    # Even a 10x spike can't exceed the population cap.
    assert cw.active_users(12.0) <= 5


def test_think_time_sample_is_clamped_and_seeded() -> None:
    think = ThinkTime(mean_s=1.0, jitter_s=0.5, min_s=0.1)
    import random as _random

    rng = _random.Random(1)
    samples = [think.sample(rng) for _ in range(1000)]
    assert all(s >= 0.1 for s in samples)
    assert 0.7 < sum(samples) / len(samples) < 1.3


def test_think_time_zero_jitter_is_constant() -> None:
    think = ThinkTime(mean_s=2.0, jitter_s=0.0)
    import random as _random

    rng = _random.Random(0)
    assert think.sample(rng) == 2.0


# --------------------------------------------------------------------------- #
# WorkloadPlan
# --------------------------------------------------------------------------- #


def test_plan_open_factory() -> None:
    plan = WorkloadPlan.open(rate_rps=10.0, duration_s=30.0)
    assert plan.kind is WorkloadKind.OPEN
    assert plan.open_model is not None
    assert plan.closed_model is None
    desc = plan.describe()
    assert desc["kind"] == "open"
    assert desc["rate_rps"] == 10.0
    assert desc["expected_arrivals"] == pytest.approx(300.0, rel=0.01)


def test_plan_closed_factory() -> None:
    plan = WorkloadPlan.closed(users=8, duration_s=20.0, think=ThinkTime(mean_s=1.5))
    assert plan.kind is WorkloadKind.CLOSED
    assert plan.closed_model is not None
    desc = plan.describe()
    assert desc["users"] == 8
    assert desc["think_mean_s"] == 1.5


def test_plan_validates_model_presence() -> None:
    with pytest.raises(ValueError):
        WorkloadPlan(kind=WorkloadKind.OPEN, duration_s=10.0)
    with pytest.raises(ValueError):
        WorkloadPlan(kind=WorkloadKind.CLOSED, duration_s=10.0)


def test_windowed_rate_tracks_arrivals() -> None:
    # 5 arrivals in [0,1), 0 in [1,2), 3 in [2,3).
    times = [0.1, 0.2, 0.3, 0.4, 0.5, 2.1, 2.2, 2.3]
    windows = dict(windowed_rate(times, window_s=1.0, duration_s=3.0))
    assert windows[0.0] == pytest.approx(5.0)
    assert windows[1.0] == pytest.approx(0.0)
    assert windows[2.0] == pytest.approx(3.0)


def test_windowed_rate_rejects_nonpositive_window() -> None:
    with pytest.raises(ValueError):
        list(windowed_rate([], window_s=0.0, duration_s=1.0))
