"""Deterministic tests for the steady-state hypothesis guard."""

from __future__ import annotations

import pytest

from app.chaos.steady_state import (
    SteadyStateHypothesis,
    availability_at_least,
    error_rate_at_most,
    latency_at_most,
)


def test_at_least_bound_holds_and_breaches() -> None:
    bound = availability_at_least(0.99)
    assert bound.check(0.995).ok
    assert bound.check(0.99).ok  # boundary is inclusive
    r = bound.check(0.98)
    assert not r.ok
    assert r.margin == pytest.approx(-0.01)


def test_at_most_bound_holds_and_breaches() -> None:
    bound = latency_at_most(1000.0)
    assert bound.check(900.0).ok
    assert bound.check(1000.0).ok
    assert not bound.check(1200.0).ok


def test_hypothesis_held_when_all_bounds_pass() -> None:
    hyp = SteadyStateHypothesis.of(
        [availability_at_least(0.99), error_rate_at_most(0.01), latency_at_most(1000.0)]
    )
    result = hyp.evaluate(
        {"availability": 0.999, "error_rate": 0.005, "p99_latency_ms": 800.0}
    )
    assert result.held
    assert result.breached == ()


def test_hypothesis_breached_names_offending_bounds() -> None:
    hyp = SteadyStateHypothesis.of(
        [availability_at_least(0.99), error_rate_at_most(0.01)]
    )
    result = hyp.evaluate({"availability": 0.90, "error_rate": 0.20})
    assert not result.held
    breached_metrics = {b.bound.metric for b in result.breached}
    assert breached_metrics == {"availability", "error_rate"}


def test_missing_metric_is_conservative_breach() -> None:
    hyp = SteadyStateHypothesis.of([availability_at_least(0.99)])
    result = hyp.evaluate({})  # metric absent
    assert not result.held
    assert result.breached[0].margin == float("-inf")


def test_empty_hypothesis_rejected() -> None:
    with pytest.raises(ValueError):
        SteadyStateHypothesis(bounds=())


def test_to_dict_round_trips_shape() -> None:
    hyp = SteadyStateHypothesis.of([availability_at_least(0.99)])
    d = hyp.evaluate({"availability": 0.999}).to_dict()
    assert d["held"] is True
    assert d["breached"] == []
    bounds = d["bounds"]
    assert isinstance(bounds, list)
    assert bounds[0]["metric"] == "availability"
