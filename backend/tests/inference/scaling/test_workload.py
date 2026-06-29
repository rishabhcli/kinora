"""Unit tests for load profiles + arrival generation (app...scaling.workload)."""

from __future__ import annotations

import pytest

from app.inference.scaling.workload import (
    ArrivalGenerator,
    BurstLoad,
    CompositeLoad,
    ConstantLoad,
    DiurnalLoad,
    RampLoad,
    RequestPriority,
    reader_population_load,
)
from app.reliability.capacity import ReadingProfile

# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #


def test_constant_is_flat() -> None:
    p = ConstantLoad(rate=2.5)
    assert p.rate_at(0.0) == 2.5
    assert p.rate_at(1e6) == 2.5
    assert p.peak_rate() == 2.5


def test_ramp_interpolates_linearly() -> None:
    p = RampLoad(start_rate=1.0, end_rate=5.0, duration_s=10.0)
    assert p.rate_at(0.0) == pytest.approx(1.0)
    assert p.rate_at(5.0) == pytest.approx(3.0)
    assert p.rate_at(10.0) == pytest.approx(5.0)
    assert p.rate_at(20.0) == pytest.approx(5.0)  # clamped past the window
    assert p.peak_rate() == 5.0


def test_diurnal_oscillates_and_stays_nonnegative() -> None:
    p = DiurnalLoad(mean_rate=3.0, amplitude=3.0, period_s=100.0, phase=0.0)
    rates = [p.rate_at(t) for t in range(0, 100, 5)]
    assert min(rates) >= 0.0
    assert max(rates) <= p.peak_rate() + 1e-9
    # Some variation across the cycle.
    assert max(rates) - min(rates) > 1.0


def test_diurnal_amplitude_cannot_exceed_mean() -> None:
    with pytest.raises(ValueError):
        DiurnalLoad(mean_rate=2.0, amplitude=3.0)


def test_burst_peaks_at_center() -> None:
    p = BurstLoad(baseline_rate=1.0, spike_rate=10.0, center_s=50.0, width_s=5.0)
    assert p.rate_at(50.0) == pytest.approx(11.0)
    assert p.rate_at(0.0) == pytest.approx(1.0, abs=0.1)
    assert p.peak_rate() == pytest.approx(11.0)


def test_composite_sums_profiles() -> None:
    p = CompositeLoad(profiles=(ConstantLoad(1.0), ConstantLoad(2.0)))
    assert p.rate_at(0.0) == pytest.approx(3.0)
    assert p.peak_rate() == pytest.approx(3.0)


# --------------------------------------------------------------------------- #
# Arrival generation
# --------------------------------------------------------------------------- #


def test_arrivals_are_deterministic_given_seed() -> None:
    g1 = ArrivalGenerator(profile=ConstantLoad(2.0), horizon_s=100.0, seed=42)
    g2 = ArrivalGenerator(profile=ConstantLoad(2.0), horizon_s=100.0, seed=42)
    a1 = g1.collect()
    a2 = g2.collect()
    assert [a.t for a in a1] == [a.t for a in a2]
    assert [a.priority for a in a1] == [a.priority for a in a2]


def test_arrival_count_tracks_rate() -> None:
    # ~2 req/s over 1000s ~= 2000 arrivals (Poisson; wide tolerance).
    g = ArrivalGenerator(profile=ConstantLoad(2.0), horizon_s=1000.0, seed=1)
    arrivals = g.collect()
    assert 1700 <= len(arrivals) <= 2300


def test_arrivals_within_horizon_and_sorted() -> None:
    g = ArrivalGenerator(profile=ConstantLoad(5.0), horizon_s=50.0, seed=3)
    arrivals = g.collect()
    assert all(0.0 <= a.t < 50.0 for a in arrivals)
    assert arrivals == sorted(arrivals, key=lambda a: a.t)


def test_committed_fraction_respected() -> None:
    g = ArrivalGenerator(
        profile=ConstantLoad(5.0), horizon_s=2000.0, committed_fraction=0.3, seed=9
    )
    arrivals = g.collect()
    committed = sum(1 for a in arrivals if a.priority is RequestPriority.COMMITTED)
    frac = committed / len(arrivals)
    assert 0.25 <= frac <= 0.35


def test_zero_rate_yields_no_arrivals() -> None:
    g = ArrivalGenerator(profile=ConstantLoad(0.0), horizon_s=100.0, seed=1)
    assert g.collect() == []


def test_burst_concentrates_arrivals() -> None:
    p = BurstLoad(baseline_rate=0.1, spike_rate=20.0, center_s=50.0, width_s=3.0)
    g = ArrivalGenerator(profile=p, horizon_s=100.0, seed=5)
    arrivals = g.collect()
    near_spike = sum(1 for a in arrivals if 40.0 <= a.t <= 60.0)
    # Most arrivals cluster around the spike.
    assert near_spike > 0.5 * len(arrivals)


# --------------------------------------------------------------------------- #
# Reader-population coupling (§4.1)
# --------------------------------------------------------------------------- #


def test_reader_population_load_uses_reading_profile() -> None:
    prof = ReadingProfile()  # 4 wps, 30 wpshot, 0.7 active
    load = reader_population_load(readers=100, profile=prof)
    expected = 100 * prof.shots_per_second * prof.active_fraction
    assert load.rate == pytest.approx(expected)


def test_reader_population_zero_readers_is_zero_rate() -> None:
    assert reader_population_load(readers=0).rate == 0.0


def test_reader_population_rejects_negative() -> None:
    with pytest.raises(ValueError):
        reader_population_load(readers=-1)
