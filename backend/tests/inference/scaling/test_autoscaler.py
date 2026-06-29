"""Unit tests for the predictive autoscaler (app.inference.scaling.autoscaler)."""

from __future__ import annotations

import pytest

from app.inference.scaling.autoscaler import (
    PredictiveAutoscaler,
    ScaleAction,
    ScalingPolicy,
)
from app.inference.scaling.contracts import BackendDescriptor, BackendKind
from app.inference.scaling.forecast import Forecast


class FakeClock:
    """A manually-advanced monotonic clock for deterministic cooldown tests."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _descriptor(concurrency: int = 1, service_time_s: float = 5.0) -> BackendDescriptor:
    return BackendDescriptor(
        backend_id="wan-i2v@gpu-a10",
        kind=BackendKind.VIDEO,
        instance_type="gpu-a10",
        concurrency=concurrency,
        service_time_s=service_time_s,
    )


def _fc(point: float, sigma: float = 0.0, horizon: int = 1, samples: int = 10) -> Forecast:
    return Forecast(point=point, sigma=sigma, horizon=horizon, samples=samples)


# --------------------------------------------------------------------------- #
# Policy validation
# --------------------------------------------------------------------------- #


def test_policy_rejects_bad_bounds() -> None:
    with pytest.raises(ValueError):
        ScalingPolicy(min_workers=5, max_workers=2)
    with pytest.raises(ValueError):
        ScalingPolicy(warm_pool=20, max_workers=4)
    with pytest.raises(ValueError):
        ScalingPolicy(max_step=0)
    with pytest.raises(ValueError):
        ScalingPolicy(headroom_quantile=1.0)


def test_policy_idle_floor_is_max_of_min_and_warmpool() -> None:
    assert ScalingPolicy(min_workers=2, warm_pool=1).idle_floor == 2
    assert ScalingPolicy(min_workers=0, warm_pool=3).idle_floor == 3


# --------------------------------------------------------------------------- #
# Forecast lookahead sizing
# --------------------------------------------------------------------------- #


def test_scales_up_on_forecast_demand() -> None:
    clock = FakeClock()
    policy = ScalingPolicy(
        warm_pool=1, max_workers=32, target_tail_s=30.0, max_step=32, scale_to_zero=False
    )
    a = PredictiveAutoscaler(_descriptor(service_time_s=5.0), policy, clock=clock)
    # Demand of 1 req/s at 5s service: needs several workers to keep tail under 30s.
    d = a.decide(forecast=_fc(1.0))
    assert d.action in (ScaleAction.FROM_ZERO, ScaleAction.UP)
    assert d.desired >= 2
    assert d.raw_target >= 1


def test_headroom_quantile_sizes_above_point() -> None:
    clock = FakeClock()
    policy = ScalingPolicy(
        headroom_quantile=0.95, warm_pool=1, max_step=64, scale_to_zero=False
    )
    a = PredictiveAutoscaler(_descriptor(), policy, clock=clock)
    d = a.decide(forecast=_fc(point=1.0, sigma=1.0))
    # Headroom demand (p95) is well above the point forecast.
    assert d.headroom_demand > 1.0


def test_concurrency_reduces_worker_count() -> None:
    clock = FakeClock()
    policy = ScalingPolicy(warm_pool=1, max_step=64, scale_to_zero=False)
    single = PredictiveAutoscaler(_descriptor(concurrency=1), policy, clock=FakeClock())
    multi = PredictiveAutoscaler(_descriptor(concurrency=4), policy, clock=clock)
    d1 = single.decide(forecast=_fc(2.0))
    d4 = multi.decide(forecast=_fc(2.0))
    assert d4.desired <= d1.desired


# --------------------------------------------------------------------------- #
# Warm-pool floor
# --------------------------------------------------------------------------- #


def test_holds_warm_pool_floor_when_idle_briefly() -> None:
    clock = FakeClock()
    policy = ScalingPolicy(warm_pool=2, scale_to_zero=True, scale_to_zero_idle_s=120.0)
    a = PredictiveAutoscaler(_descriptor(), policy, clock=clock, current=2)
    d = a.decide(forecast=_fc(0.0))
    assert d.desired == 2  # idle but not long enough => hold capacity pre-collapse
    assert "pre-scale-to-zero" in d.reason


# --------------------------------------------------------------------------- #
# Scale-to-zero
# --------------------------------------------------------------------------- #


def test_scales_to_zero_after_sustained_idle() -> None:
    clock = FakeClock()
    policy = ScalingPolicy(
        warm_pool=0, min_workers=0, scale_to_zero=True, scale_to_zero_idle_s=100.0
    )
    a = PredictiveAutoscaler(_descriptor(), policy, clock=clock, current=4)
    # First idle tick: starts the idle timer, holds the floor (0 here).
    a.decide(forecast=_fc(0.0))
    clock.advance(101.0)
    d = a.decide(forecast=_fc(0.0))
    assert d.desired == 0
    assert d.action is ScaleAction.TO_ZERO


def test_scale_to_zero_disabled_holds_floor() -> None:
    clock = FakeClock()
    policy = ScalingPolicy(min_workers=1, warm_pool=1, scale_to_zero=False)
    a = PredictiveAutoscaler(_descriptor(), policy, clock=clock, current=1)
    a.decide(forecast=_fc(0.0))
    clock.advance(10_000.0)
    d = a.decide(forecast=_fc(0.0))
    assert d.desired == 1  # never collapses below floor


def test_scale_to_zero_disabled_scales_in_to_floor() -> None:
    # With scale-to-zero off and a pool above its floor, an idle tick scales *in*
    # toward the warm-pool floor (after the down cooldown), never below it.
    clock = FakeClock()
    policy = ScalingPolicy(
        min_workers=1, warm_pool=1, scale_to_zero=False, scale_down_cooldown_s=0.0, max_step=64
    )
    a = PredictiveAutoscaler(_descriptor(), policy, clock=clock, current=6)
    d = a.decide(forecast=_fc(0.0))
    assert d.desired == 1
    assert d.action is ScaleAction.DOWN


def test_warm_pool_keeps_standby_through_idle() -> None:
    clock = FakeClock()
    policy = ScalingPolicy(
        min_workers=0, warm_pool=1, scale_to_zero=True, scale_to_zero_idle_s=50.0
    )
    a = PredictiveAutoscaler(_descriptor(), policy, clock=clock, current=3)
    a.decide(forecast=_fc(0.0))
    clock.advance(60.0)
    d = a.decide(forecast=_fc(0.0))
    # warm_pool=1 but scale_to_zero true: idle_floor is 1, yet to-zero collapses
    # fully past the idle window. Warm-pool floor only applies when NOT to-zero.
    assert d.desired == 0


# --------------------------------------------------------------------------- #
# From-zero (cold start trigger)
# --------------------------------------------------------------------------- #


def test_warms_from_zero_on_returning_demand() -> None:
    clock = FakeClock()
    policy = ScalingPolicy(
        min_workers=0, warm_pool=0, scale_to_zero=True, max_step=64, scale_to_zero_idle_s=10.0
    )
    a = PredictiveAutoscaler(_descriptor(), policy, clock=clock, current=0)
    d = a.decide(forecast=_fc(1.0))
    assert d.action is ScaleAction.FROM_ZERO
    assert d.desired >= 1


def test_from_zero_bypasses_down_cooldown() -> None:
    # A from-zero warm must not be blocked by a recent scale event.
    clock = FakeClock()
    policy = ScalingPolicy(
        min_workers=0,
        warm_pool=0,
        scale_to_zero=True,
        scale_down_cooldown_s=300.0,
        max_step=64,
        scale_to_zero_idle_s=5.0,
    )
    a = PredictiveAutoscaler(_descriptor(), policy, clock=clock, current=4)
    a.decide(forecast=_fc(0.0))
    clock.advance(6.0)
    a.decide(forecast=_fc(0.0))  # to-zero
    clock.advance(1.0)
    d = a.decide(forecast=_fc(1.0))  # demand returns immediately after
    assert d.desired >= 1
    assert d.action is ScaleAction.FROM_ZERO


# --------------------------------------------------------------------------- #
# Anti-flap: asymmetric cooldown + step limiting
# --------------------------------------------------------------------------- #


def test_scale_up_is_immediate() -> None:
    clock = FakeClock()
    policy = ScalingPolicy(
        warm_pool=1, scale_up_cooldown_s=0.0, max_step=64, scale_to_zero=False
    )
    a = PredictiveAutoscaler(_descriptor(), policy, clock=clock, current=1)
    d = a.decide(forecast=_fc(3.0))
    assert d.desired > 1  # no cooldown gate on the way up


def test_scale_down_waits_out_cooldown() -> None:
    clock = FakeClock()
    policy = ScalingPolicy(
        warm_pool=1, scale_down_cooldown_s=90.0, max_step=64, scale_to_zero=False
    )
    a = PredictiveAutoscaler(_descriptor(), policy, clock=clock, current=8)
    # Scale up first (sets last_scale_at), then demand drops.
    a.decide(forecast=_fc(5.0))
    d_hold = a.decide(forecast=_fc(0.2))  # within cooldown => hold
    assert d_hold.desired == a.current  # not reduced yet
    held = d_hold.desired
    clock.advance(91.0)
    d_down = a.decide(forecast=_fc(0.2))
    assert d_down.desired < held  # cooldown elapsed => allowed down


def test_step_limit_caps_growth_per_tick() -> None:
    clock = FakeClock()
    policy = ScalingPolicy(warm_pool=1, max_step=2, scale_to_zero=False)
    a = PredictiveAutoscaler(_descriptor(), policy, clock=clock, current=1)
    d = a.decide(forecast=_fc(10.0))  # would want many workers
    assert d.desired == 3  # current 1 + max_step 2


def test_max_workers_clamps_target() -> None:
    clock = FakeClock()
    policy = ScalingPolicy(max_workers=3, warm_pool=1, max_step=64, scale_to_zero=False)
    a = PredictiveAutoscaler(_descriptor(), policy, clock=clock, current=1)
    d = a.decide(forecast=_fc(50.0))
    assert d.desired <= 3


def test_observed_warm_reconciles_drift() -> None:
    clock = FakeClock()
    policy = ScalingPolicy(warm_pool=1, max_step=64, scale_to_zero=False)
    a = PredictiveAutoscaler(_descriptor(), policy, clock=clock, current=2)
    # Orchestrator actually lost a worker; reconcile to 1 before deciding.
    d = a.decide(forecast=_fc(0.0), observed_warm=1)
    assert d.previous == 1


def test_decision_to_dict_round_trip() -> None:
    clock = FakeClock()
    policy = ScalingPolicy(warm_pool=1, max_step=64, scale_to_zero=False)
    a = PredictiveAutoscaler(_descriptor(), policy, clock=clock, current=1)
    d = a.decide(forecast=_fc(2.0))
    payload = d.to_dict()
    assert payload["backend_id"] == "wan-i2v@gpu-a10"
    assert payload["desired"] == d.desired
    assert payload["action"] in {a.value for a in ScaleAction}
