"""Demand-signal model tests (kinora.md §4.5–§4.10).

Pure functions of an immutable snapshot — no clock, no infra.
"""

from __future__ import annotations

import math

from app.autoscale.lanes import Lane, QoSClass
from app.autoscale.signal import DemandSnapshot, SessionDemand, percentile


def test_percentile_linear_interpolation() -> None:
    assert percentile([], 0.95) == 0.0
    assert percentile([7.0], 0.95) == 7.0
    assert percentile([0.0, 10.0], 0.5) == 5.0
    # p95 of 1..100 ~ 95.05 with linear interpolation.
    p95 = percentile([float(i) for i in range(1, 101)], 0.95)
    assert 94.0 <= p95 <= 96.0


def test_session_seconds_to_dry_and_risk() -> None:
    # 6 words per video-second, reading 12 wps -> consuming 2 film-seconds/wall-second.
    s = SessionDemand(velocity_wps=12.0, committed_seconds_ahead=4.0, words_per_video_second=6.0)
    assert s.film_consumption_rate() == 2.0
    assert s.seconds_to_dry() == 2.0  # 4s buffer / 2 = 2s to dry
    # Will dry in 2s, horizon 30s -> high risk.
    assert s.underrun_risk(horizon_s=30.0) > 0.9


def test_idle_session_has_no_risk_or_demand() -> None:
    s = SessionDemand(velocity_wps=12.0, committed_seconds_ahead=0.0, idle=True)
    assert s.film_consumption_rate() == 0.0
    assert math.isinf(s.seconds_to_dry())
    assert s.underrun_risk() == 0.0
    assert s.shots_needed() == 0.0


def test_well_buffered_session_has_zero_risk() -> None:
    s = SessionDemand(velocity_wps=2.0, committed_seconds_ahead=120.0, words_per_video_second=6.0)
    assert s.underrun_risk(horizon_s=30.0) == 0.0
    # Buffered way past horizon demand -> no shots needed.
    assert s.shots_needed(horizon_s=30.0) == 0.0


def test_lane_pressure_routes_qos_to_lanes() -> None:
    snap = DemandSnapshot(
        depth_by_qos={QoSClass.COMMITTED: 6, QoSClass.SPECULATIVE: 4, QoSClass.KEYFRAME: 2},
        inflight_by_lane={Lane.PROVIDER: 3},
        latency_samples_s={Lane.PROVIDER: [10.0, 20.0, 30.0]},
        sessions=(),
    )
    pressures = snap.lane_pressures([Lane.PROVIDER, Lane.CPU, Lane.GPU])
    # All QoS classes route to the provider lane in the default mapping.
    assert pressures[Lane.PROVIDER].queue_depth == 12
    assert pressures[Lane.CPU].queue_depth == 0
    # p95 latency drives latency pressure.
    assert pressures[Lane.PROVIDER].p95_latency_s > 25.0
    assert pressures[Lane.PROVIDER].latency_pressure > 1.0


def test_lookahead_demand_adds_to_committed_lane_backlog() -> None:
    sessions = tuple(
        SessionDemand(velocity_wps=12.0, committed_seconds_ahead=0.0) for _ in range(3)
    )
    snap = DemandSnapshot(
        depth_by_qos={QoSClass.COMMITTED: 0},
        inflight_by_lane={},
        latency_samples_s={},
        sessions=sessions,
    )
    pressures = snap.lane_pressures([Lane.PROVIDER])
    # No realised depth, but look-ahead demand makes effective backlog positive.
    assert pressures[Lane.PROVIDER].effective_backlog > 0.0
    assert pressures[Lane.PROVIDER].underrun_pressure > 0.0


def test_provider_saturation_from_inflight_vs_quota() -> None:
    snap = DemandSnapshot(
        depth_by_qos={QoSClass.COMMITTED: 10},
        inflight_by_lane={Lane.PROVIDER: 16},
        latency_samples_s={Lane.PROVIDER: [5.0]},
        provider_quota=16,
    )
    p = snap.lane_pressures([Lane.PROVIDER])[Lane.PROVIDER]
    assert p.provider_saturation == 1.0
