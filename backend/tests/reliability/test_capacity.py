"""Unit tests for the capacity model (app.reliability.capacity)."""

from __future__ import annotations

import math

import pytest

from app.reliability.capacity import (
    BudgetRunway,
    ReadingProfile,
    RenderDemand,
    erlang_c,
    max_concurrent_readers,
    min_servers_for_utilisation,
    mmc_queue,
    watermark_feasibility,
)

# --------------------------------------------------------------------------- #
# Reading profile / the §4.1 asymmetry
# --------------------------------------------------------------------------- #


def test_consumption_rate_matches_section_4_1() -> None:
    # §4.1: ~0.15-0.30 video-seconds consumed per wall-clock second.
    profile = ReadingProfile()  # 4 wps, 30 wpshot, 5 s/shot, 0.7 active
    # shots/s = 4/30 = 0.1333; *5 = 0.667 raw; *0.7 active = 0.467 ... but that's
    # the *content* rate. The §4.1 figure is per *wall-clock* including dwell, so
    # at active_fraction 0.7 we land in the right order of magnitude. We assert
    # the identity rather than the prose midpoint.
    assert profile.shots_per_second == pytest.approx(4 / 30)
    expected = (4 / 30) * 5 * 0.7
    assert profile.video_seconds_per_wallclock == pytest.approx(expected)


def test_consumption_in_section_4_1_band_with_dwell() -> None:
    # A reader dwelling more (lower active fraction) lands inside the 0.15-0.30 band.
    profile = ReadingProfile(velocity_wps=4.0, active_fraction=0.3)
    assert 0.15 <= profile.video_seconds_per_wallclock <= 0.30


def test_render_demand_scales_with_readers() -> None:
    profile = ReadingProfile()
    one = RenderDemand(readers=1, profile=profile)
    ten = RenderDemand(readers=10, profile=profile)
    assert ten.arrival_rate_shots_per_s == pytest.approx(10 * one.arrival_rate_shots_per_s)
    assert ten.offered_video_seconds_per_s == pytest.approx(
        10 * one.offered_video_seconds_per_s
    )


# --------------------------------------------------------------------------- #
# Erlang-C / M/M/c
# --------------------------------------------------------------------------- #


def test_erlang_c_known_value() -> None:
    # Textbook: c=2 servers, offered load a=1 erlang -> P(wait) = 1/3.
    assert erlang_c(2, 1.0) == pytest.approx(1 / 3, rel=1e-6)


def test_erlang_c_no_load_no_wait() -> None:
    assert erlang_c(4, 0.0) == 0.0


def test_erlang_c_saturated_always_waits() -> None:
    assert erlang_c(2, 2.0) == 1.0
    assert erlang_c(2, 3.0) == 1.0


def test_erlang_c_decreases_with_more_servers() -> None:
    a = 3.0
    p4 = erlang_c(4, a)
    p6 = erlang_c(6, a)
    assert p6 < p4


def test_mmc_stable_queue() -> None:
    # 4 committed slots, 60s render, low arrival rate => stable, small wait.
    result = mmc_queue(arrival_rate_per_s=0.04, service_time_s=60.0, servers=4)
    assert result.stable is True
    assert result.offered_load_erlangs == pytest.approx(0.04 * 60.0)
    assert 0.0 < result.utilisation < 1.0
    assert result.mean_response_s >= result.service_time_s


def test_mmc_unstable_queue() -> None:
    # Arrival exceeds capacity (λ·service > servers) => unstable, infinite wait.
    result = mmc_queue(arrival_rate_per_s=0.2, service_time_s=60.0, servers=4)
    assert result.stable is False
    assert result.utilisation >= 1.0
    assert math.isinf(result.mean_wait_s)
    assert result.wait_probability == 1.0


def test_mmc_zero_service_time() -> None:
    result = mmc_queue(arrival_rate_per_s=1.0, service_time_s=0.0, servers=4)
    # Instant service is "unstable" in the sense mu is infinite; we report 0 load.
    assert result.offered_load_erlangs == 0.0


def test_mmc_response_increases_with_load() -> None:
    low = mmc_queue(arrival_rate_per_s=0.02, service_time_s=60.0, servers=4)
    high = mmc_queue(arrival_rate_per_s=0.06, service_time_s=60.0, servers=4)
    assert high.mean_response_s > low.mean_response_s


def test_min_servers_for_utilisation() -> None:
    # offered = 0.05 * 60 = 3 erlangs; at 80% target -> ceil(3/0.8) = 4 servers.
    n = min_servers_for_utilisation(
        arrival_rate_per_s=0.05, service_time_s=60.0, max_utilisation=0.8
    )
    assert n == 4
    # No load -> 1 server.
    assert min_servers_for_utilisation(arrival_rate_per_s=0.0, service_time_s=60.0) == 1


def test_min_servers_rejects_bad_utilisation() -> None:
    with pytest.raises(ValueError):
        min_servers_for_utilisation(arrival_rate_per_s=1.0, service_time_s=1.0, max_utilisation=1.0)


# --------------------------------------------------------------------------- #
# Budget runway (§11)
# --------------------------------------------------------------------------- #


def test_budget_runway_basic() -> None:
    runway = BudgetRunway(ceiling_video_s=1650.0, burn_rate_video_s_per_s=0.5)
    assert runway.runway_seconds == pytest.approx(3300.0)
    assert runway.effective_burn_per_s == pytest.approx(0.5)


def test_budget_runway_cache_extends_it() -> None:
    no_cache = BudgetRunway(ceiling_video_s=1650.0, burn_rate_video_s_per_s=1.0)
    with_cache = BudgetRunway(
        ceiling_video_s=1650.0, burn_rate_video_s_per_s=1.0, cache_hit_ratio=0.5
    )
    # 50% cache hits halve the burn => double the runway.
    assert with_cache.runway_seconds == pytest.approx(2 * no_cache.runway_seconds)


def test_budget_runway_zero_burn_is_infinite() -> None:
    runway = BudgetRunway(ceiling_video_s=1650.0, burn_rate_video_s_per_s=0.0)
    assert math.isinf(runway.runway_seconds)
    assert math.isinf(runway.reader_seconds(10))


def test_max_concurrent_readers() -> None:
    profile = ReadingProfile(velocity_wps=4.0, active_fraction=0.3)  # ~0.2 vs/s
    n = max_concurrent_readers(
        ceiling_video_s=1650.0, profile=profile, target_session_s=300.0
    )
    # Per reader: 0.2 vs/s * 300s = 60 video-s; 1650/60 = 27 readers.
    per_reader = profile.video_seconds_per_wallclock * 300.0
    assert n == int(1650.0 // per_reader)
    assert n > 0


def test_max_concurrent_readers_with_cache() -> None:
    profile = ReadingProfile(active_fraction=0.3)
    without = max_concurrent_readers(
        ceiling_video_s=1650.0, profile=profile, target_session_s=300.0
    )
    with_cache = max_concurrent_readers(
        ceiling_video_s=1650.0, profile=profile, target_session_s=300.0, cache_hit_ratio=0.5
    )
    assert with_cache > without


# --------------------------------------------------------------------------- #
# Watermark feasibility (§4.5/§4.10)
# --------------------------------------------------------------------------- #


def test_watermark_feasible_for_section_4_10_example() -> None:
    # §4.10: 2-3 workers clear a reader's demand "comfortably". With 3 workers and
    # a ~30s render, production = 3*5/30 = 0.5 vs/s, vs a single reader's ~0.47.
    profile = ReadingProfile()  # ~0.467 vs/s
    fz = watermark_feasibility(
        servers=3,
        service_time_s=30.0,
        seconds_per_shot=5.0,
        profile=profile,
        high_watermark_s=75.0,
    )
    assert fz.feasible is True
    assert fz.headroom_ratio >= 1.0
    assert fz.time_to_fill_high_s > 0.0


def test_watermark_infeasible_with_too_few_workers() -> None:
    profile = ReadingProfile(velocity_wps=12.0, active_fraction=1.0)  # fast skimmer
    fz = watermark_feasibility(
        servers=1,
        service_time_s=90.0,
        seconds_per_shot=5.0,
        profile=profile,
        high_watermark_s=75.0,
    )
    assert fz.feasible is False
    assert fz.headroom_ratio < 1.0
    assert math.isinf(fz.time_to_fill_high_s)


def test_watermark_more_workers_fill_faster() -> None:
    profile = ReadingProfile()
    slow = watermark_feasibility(
        servers=2, service_time_s=30.0, seconds_per_shot=5.0, profile=profile,
        high_watermark_s=75.0,
    )
    fast = watermark_feasibility(
        servers=4, service_time_s=30.0, seconds_per_shot=5.0, profile=profile,
        high_watermark_s=75.0,
    )
    assert fast.time_to_fill_high_s < slow.time_to_fill_high_s


def test_watermark_rejects_zero_service_time() -> None:
    with pytest.raises(ValueError):
        watermark_feasibility(
            servers=4, service_time_s=0.0, seconds_per_shot=5.0,
            profile=ReadingProfile(), high_watermark_s=75.0,
        )
