"""Unit tests for queue-theory sizing (app.inference.scaling.queueing)."""

from __future__ import annotations

import math

import pytest

from app.inference.scaling.queueing import (
    mmc_response_quantile_s,
    mmc_wait_quantile_s,
    servers_for_response_target,
    servers_for_tail_target,
    size_fleet,
)


def test_wait_quantile_zero_when_lightly_loaded() -> None:
    # Very light load: p50 wait is zero (most arrivals find a free server).
    w = mmc_wait_quantile_s(
        arrival_rate_per_s=0.1, service_time_s=1.0, servers=4, quantile=0.5
    )
    assert w == 0.0


def test_wait_quantile_positive_under_load() -> None:
    # Heavier load: the p99 wait is positive.
    w = mmc_wait_quantile_s(
        arrival_rate_per_s=3.5, service_time_s=1.0, servers=4, quantile=0.99
    )
    assert w > 0.0


def test_wait_quantile_infinite_when_unstable() -> None:
    # Offered load 5 erlangs on 4 servers => unstable.
    w = mmc_wait_quantile_s(
        arrival_rate_per_s=5.0, service_time_s=1.0, servers=4, quantile=0.9
    )
    assert w == math.inf


def test_wait_quantile_monotonic_in_quantile() -> None:
    w90 = mmc_wait_quantile_s(
        arrival_rate_per_s=3.0, service_time_s=1.0, servers=4, quantile=0.90
    )
    w99 = mmc_wait_quantile_s(
        arrival_rate_per_s=3.0, service_time_s=1.0, servers=4, quantile=0.99
    )
    assert w99 >= w90


def test_more_servers_lower_wait() -> None:
    w4 = mmc_wait_quantile_s(
        arrival_rate_per_s=3.0, service_time_s=1.0, servers=4, quantile=0.99
    )
    w8 = mmc_wait_quantile_s(
        arrival_rate_per_s=3.0, service_time_s=1.0, servers=8, quantile=0.99
    )
    assert w8 <= w4


def test_response_quantile_includes_service_time() -> None:
    w = mmc_wait_quantile_s(
        arrival_rate_per_s=0.1, service_time_s=2.0, servers=4, quantile=0.5
    )
    r = mmc_response_quantile_s(
        arrival_rate_per_s=0.1, service_time_s=2.0, servers=4, quantile=0.5
    )
    assert r == pytest.approx(w + 2.0)


def test_servers_for_response_target_finds_min() -> None:
    c = servers_for_response_target(
        arrival_rate_per_s=2.0, service_time_s=1.0, target_response_s=1.5
    )
    # With lambda=2, mu=1, need at least 3 servers for stability; check it meets target.
    from app.reliability.capacity import mmc_queue

    assert mmc_queue(arrival_rate_per_s=2.0, service_time_s=1.0, servers=c).mean_response_s <= 1.5
    # And c-1 does not.
    smaller = mmc_queue(arrival_rate_per_s=2.0, service_time_s=1.0, servers=c - 1)
    assert (not smaller.stable) or smaller.mean_response_s > 1.5


def test_response_target_below_service_time_raises() -> None:
    with pytest.raises(ValueError, match="below service_time_s"):
        servers_for_response_target(
            arrival_rate_per_s=1.0, service_time_s=5.0, target_response_s=2.0
        )


def test_tail_target_needs_more_servers_than_mean() -> None:
    mean_c = servers_for_response_target(
        arrival_rate_per_s=3.0, service_time_s=1.0, target_response_s=2.0
    )
    tail_c = servers_for_tail_target(
        arrival_rate_per_s=3.0, service_time_s=1.0, target_response_s=2.0, quantile=0.99
    )
    assert tail_c >= mean_c


def test_size_fleet_meets_tail_target() -> None:
    sizing = size_fleet(
        arrival_rate_per_s=2.0, service_time_s=1.0, target_response_s=3.0, quantile=0.95
    )
    assert sizing.meets_target
    assert sizing.achieved_tail_s <= 3.0
    assert sizing.queueing.stable
    d = sizing.to_dict()
    assert d["meets_target"] is True
    assert d["servers"] == sizing.servers


def test_size_fleet_impossible_target_raises() -> None:
    with pytest.raises(ValueError):
        size_fleet(
            arrival_rate_per_s=2.0, service_time_s=1.0, target_response_s=0.5, quantile=0.99
        )


def test_size_fleet_unsatisfiable_within_max_raises() -> None:
    with pytest.raises(ValueError, match="no server count"):
        servers_for_tail_target(
            arrival_rate_per_s=100.0,
            service_time_s=1.0,
            target_response_s=1.01,
            quantile=0.999,
            max_servers=2,
        )
