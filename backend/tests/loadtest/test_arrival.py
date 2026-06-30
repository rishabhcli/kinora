"""Arrival-process shape: constant / ramp / spike counts + Poisson statistics."""

from __future__ import annotations

import random
import statistics

import pytest

from app.loadtest.arrival import (
    ArrivalShape,
    RateEnvelope,
    deterministic_arrivals,
    make_schedule,
    poisson_arrivals,
)


def test_constant_deterministic_count_and_spacing() -> None:
    env = RateEnvelope(ArrivalShape.CONSTANT, duration_s=10.0, base_rate=5.0)
    times = deterministic_arrivals(env)
    # 5 req/s * 10 s = 50 arrivals.
    assert len(times) == 50
    # Evenly spaced at 0.2 s.
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert all(g == pytest.approx(0.2, abs=1e-6) for g in gaps)


def test_ramp_local_rate_increases() -> None:
    env = RateEnvelope(
        ArrivalShape.RAMP, duration_s=10.0, start_rate=1.0, end_rate=11.0
    )
    times = deterministic_arrivals(env)
    # Mean rate is 6/s over 10 s ⇒ ~60 arrivals.
    assert env.total_expected() == pytest.approx(60.0)
    assert abs(len(times) - 60) <= 1
    # Density rises: gaps early are larger than gaps late.
    early_gap = times[1] - times[0]
    late_gap = times[-1] - times[-2]
    assert late_gap < early_gap


def test_spike_concentrates_arrivals_in_window() -> None:
    env = RateEnvelope(
        ArrivalShape.SPIKE,
        duration_s=20.0,
        base_rate=1.0,
        peak_rate=20.0,
        spike_start_s=8.0,
        spike_end_s=10.0,
    )
    times = deterministic_arrivals(env)
    in_window = [t for t in times if 8.0 <= t <= 10.0]
    # Peak 20/s over the 2 s window ≈ 40 arrivals concentrated there.
    assert len(in_window) >= 30
    # The 2 s window holds far more arrivals than any other 2 s slice.
    out_window = [t for t in times if t < 8.0]
    assert len(in_window) > len(out_window)


def test_expected_count_matches_envelope_integral() -> None:
    env = RateEnvelope(ArrivalShape.CONSTANT, duration_s=4.0, base_rate=3.0)
    assert env.expected_count(2.0) == pytest.approx(6.0)
    assert env.total_expected() == pytest.approx(12.0)

    ramp = RateEnvelope(ArrivalShape.RAMP, duration_s=2.0, start_rate=0.0, end_rate=4.0)
    # ∫ 0..2 of (2 t) dt = t^2 |0..2 = 4.
    assert ramp.total_expected() == pytest.approx(4.0)


def test_poisson_mean_count_matches_rate() -> None:
    env = RateEnvelope(ArrivalShape.CONSTANT, duration_s=100.0, base_rate=10.0)
    counts = []
    for seed in range(40):
        rng = random.Random(seed)
        counts.append(len(poisson_arrivals(env, rng)))
    mean = statistics.mean(counts)
    # Expected 1000 arrivals; the empirical mean is close.
    assert mean == pytest.approx(1000.0, rel=0.05)


def test_poisson_interarrival_is_exponential_mean() -> None:
    env = RateEnvelope(ArrivalShape.CONSTANT, duration_s=2000.0, base_rate=5.0)
    rng = random.Random(123)
    times = poisson_arrivals(env, rng)
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    # Mean gap ≈ 1/λ = 0.2 s.
    assert statistics.mean(gaps) == pytest.approx(0.2, rel=0.1)


def test_poisson_thinning_tracks_spike_shape() -> None:
    env = RateEnvelope(
        ArrivalShape.SPIKE,
        duration_s=60.0,
        base_rate=1.0,
        peak_rate=30.0,
        spike_start_s=20.0,
        spike_end_s=25.0,
    )
    rng = random.Random(5)
    times = poisson_arrivals(env, rng)
    in_window = [t for t in times if 20.0 <= t <= 25.0]
    # 5 s at 30/s ≈ 150 expected in the burst; should dominate.
    assert len(in_window) > 0.4 * len(times)


def test_poisson_is_reproducible_for_a_seed() -> None:
    env = RateEnvelope(ArrivalShape.CONSTANT, duration_s=50.0, base_rate=8.0)
    a = make_schedule(env, poisson=True, rng=random.Random(77))
    b = make_schedule(env, poisson=True, rng=random.Random(77))
    assert a == b


def test_envelope_validation() -> None:
    with pytest.raises(ValueError):
        RateEnvelope(ArrivalShape.CONSTANT, duration_s=0.0)
    with pytest.raises(ValueError):
        RateEnvelope(ArrivalShape.SPIKE, duration_s=10.0)  # missing peak_rate
    with pytest.raises(ValueError):
        RateEnvelope(
            ArrivalShape.SPIKE,
            duration_s=10.0,
            peak_rate=5.0,
            spike_start_s=8.0,
            spike_end_s=5.0,  # end < start
        )
