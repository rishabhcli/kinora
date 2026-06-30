"""Latency-budget pass/fail gate on crafted collectors."""

from __future__ import annotations

import pytest

from app.loadtest.budget import (
    EndpointBudget,
    LatencyBasis,
    LatencyBudget,
    default_kinora_budget,
    evaluate_budget,
)
from app.loadtest.collector import LatencyCollector
from app.loadtest.target import LoadResponse, Outcome


def _fill(
    c: LatencyCollector,
    endpoint: str,
    latency_s: float,
    n: int,
    outcome: Outcome = Outcome.OK,
) -> None:
    for i in range(n):
        t = i * 0.001
        c.record(
            LoadResponse(endpoint=endpoint, outcome=outcome, latency_s=latency_s, status=200),
            intended_s=t,
            finish_s=t + latency_s,
        )


def test_gate_passes_when_under_budget() -> None:
    c = LatencyCollector(correct_omission=False)
    _fill(c, "page_turn", 0.05, 500)  # 50 ms, well under a 250 ms p95 budget
    budget = LatencyBudget(
        endpoints={"page_turn": EndpointBudget("page_turn", {"p95": 0.25, "p99": 0.6})}
    )
    result = evaluate_budget(c, budget)
    assert result.passed
    assert not result.violations


def test_gate_fails_on_latency_violation() -> None:
    c = LatencyCollector(correct_omission=False)
    _fill(c, "page_turn", 0.4, 500)  # 400 ms blows the 250 ms p95
    budget = LatencyBudget(
        endpoints={"page_turn": EndpointBudget("page_turn", {"p95": 0.25})}
    )
    result = evaluate_budget(c, budget)
    assert not result.passed
    v = result.violations[0]
    assert v.endpoint == "page_turn" and v.metric == "p95"
    assert v.observed > v.threshold


def test_gate_fails_on_error_rate() -> None:
    c = LatencyCollector(correct_omission=False)
    _fill(c, "jump", 0.05, 90, outcome=Outcome.OK)
    _fill(c, "jump", 0.05, 10, outcome=Outcome.ERROR)  # 10% errors
    budget = LatencyBudget(
        endpoints={"jump": EndpointBudget("jump", {"p95": 1.0}, max_error_rate=0.02)}
    )
    result = evaluate_budget(c, budget)
    assert not result.passed
    assert any(v.metric == "error_rate" for v in result.violations)


def test_gate_flags_missing_endpoint_but_can_still_pass() -> None:
    c = LatencyCollector(correct_omission=False)
    _fill(c, "page_turn", 0.05, 100)
    budget = LatencyBudget(
        endpoints={
            "page_turn": EndpointBudget("page_turn", {"p95": 0.25}),
            "jump": EndpointBudget("jump", {"p95": 0.4}),  # never exercised
        }
    )
    result = evaluate_budget(c, budget)
    assert result.passed  # missing endpoint is informational, not a failure
    assert "jump" in result.missing_endpoints


def test_aggregate_budget_checked() -> None:
    c = LatencyCollector(correct_omission=False)
    _fill(c, "a", 2.0, 100)  # slow
    budget = LatencyBudget(
        endpoints={},
        aggregate=EndpointBudget("__all__", {"p99": 1.0}),
    )
    result = evaluate_budget(c, budget)
    assert not result.passed
    assert any(v.endpoint == "__all__" for v in result.violations)


def test_basis_service_vs_corrected() -> None:
    c = LatencyCollector(correct_omission=False)
    # Service latency 50 ms, but each finished 1 s after intended (queued).
    for i in range(100):
        c.record(
            LoadResponse(endpoint="x", outcome=Outcome.OK, latency_s=0.05, status=200),
            intended_s=float(i),
            finish_s=float(i) + 1.0,
        )
    svc_budget = LatencyBudget(
        endpoints={"x": EndpointBudget("x", {"p95": 0.1})}, basis=LatencyBasis.SERVICE
    )
    corr_budget = LatencyBudget(
        endpoints={"x": EndpointBudget("x", {"p95": 0.1})}, basis=LatencyBasis.CORRECTED
    )
    assert evaluate_budget(c, svc_budget).passed  # service is fast
    assert not evaluate_budget(c, corr_budget).passed  # corrected shows the wait


def test_unknown_percentile_key_rejected() -> None:
    with pytest.raises(ValueError):
        EndpointBudget("x", {"p42": 0.1})


def test_default_kinora_budget_is_well_formed() -> None:
    budget = default_kinora_budget()
    assert "page_turn" in budget.endpoints
    assert budget.aggregate is not None
    assert budget.basis is LatencyBasis.CORRECTED
