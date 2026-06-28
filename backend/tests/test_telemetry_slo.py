"""Telemetry: SLO objects + multi-window burn-rate math."""

from __future__ import annotations

import math

import pytest

from app.telemetry.slo import (
    SLO,
    SLOEvaluation,
    SLOKind,
    default_slos,
    parse_duration,
    slo_catalogue,
    standard_burn_windows,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("30s", 30), ("5m", 300), ("1h", 3600), ("1d", 86400), ("1w", 604800)],
)
def test_parse_duration(value: str, expected: float) -> None:
    assert parse_duration(value) == expected


def test_parse_duration_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_duration("5x")
    with pytest.raises(ValueError):
        parse_duration("")


def test_error_budget_is_one_minus_objective() -> None:
    slo = SLO("x", "x", SLOKind.AVAILABILITY, objective=0.995)
    assert abs(slo.error_budget - 0.005) < 1e-12


def test_burn_rate_at_budget_is_one() -> None:
    slo = SLO("x", "x", SLOKind.AVAILABILITY, objective=0.99)  # budget = 0.01
    # Spending exactly the budget fraction → burn rate 1.0.
    assert abs(slo.burn_rate(0.01) - 1.0) < 1e-9
    # Double the budget → burn rate 2.0.
    assert abs(slo.burn_rate(0.02) - 2.0) < 1e-9
    # No bad events → no burn.
    assert slo.burn_rate(0.0) == 0.0


def test_burn_rate_with_perfect_objective_is_inf_on_any_error() -> None:
    slo = SLO("perfect", "p", SLOKind.AVAILABILITY, objective=1.0)
    assert slo.burn_rate(0.0) == 0.0
    assert math.isinf(slo.burn_rate(0.001))


def test_budget_remaining_clamps() -> None:
    slo = SLO("x", "x", SLOKind.AVAILABILITY, objective=0.99)  # budget = 0.01
    assert abs(slo.budget_remaining(0.0) - 1.0) < 1e-9
    assert abs(slo.budget_remaining(0.005) - 0.5) < 1e-9
    assert slo.budget_remaining(0.02) == 0.0  # over budget → clamped at 0


def test_is_breaching() -> None:
    slo = SLO("x", "x", SLOKind.AVAILABILITY, objective=0.99)
    assert slo.is_breaching(0.98)
    assert not slo.is_breaching(0.999)


def test_evaluation_dataclass() -> None:
    slo = SLO("api", "a", SLOKind.AVAILABILITY, objective=0.995)
    ev = SLOEvaluation.evaluate(slo, good_ratio=0.99)
    assert ev.slo == "api"
    assert abs(ev.bad_ratio - 0.01) < 1e-9
    assert abs(ev.burn_rate - 2.0) < 1e-9  # 0.01 / 0.005
    assert ev.breaching is True
    d = ev.to_dict()
    assert d["slo"] == "api"
    assert d["breaching"] is True


def test_evaluation_serializes_inf_burn_rate() -> None:
    slo = SLO("perfect", "p", SLOKind.AVAILABILITY, objective=1.0)
    ev = SLOEvaluation.evaluate(slo, good_ratio=0.5)
    assert ev.to_dict()["burn_rate"] == "inf"


def test_standard_burn_windows_are_descending_urgency() -> None:
    windows = standard_burn_windows()
    rates = [w.burn_rate for w in windows]
    assert rates == sorted(rates, reverse=True)  # fast tier burns fastest
    assert windows[0].severity == "page"
    assert windows[-1].severity == "ticket"


def test_burn_window_budget_consumed_fraction() -> None:
    windows = standard_burn_windows()
    fast = windows[0]
    # 14.4x over 1h against a 30d budget ≈ 2% of the budget consumed in that hour.
    frac = fast.budget_consumed_fraction()
    assert 0.0 < frac < 0.05


def test_default_slos_cover_the_required_signals() -> None:
    names = {s.name for s in default_slos()}
    # RED (api), USE (jobs), and the §13 quality signals.
    assert {"api_availability", "api_latency", "render_job_success"} <= names
    assert {"buffer_health", "qa_pass_rate", "ccs_quality"} <= names


def test_default_slos_reference_real_metric_names() -> None:
    by_name = {s.name: s for s in default_slos()}
    assert "kinora_http_requests_total" in by_name["api_availability"].sli_query
    assert "kinora_jobs_total" in by_name["render_job_success"].sli_query
    assert "kinora_shots_accepted_total" in by_name["qa_pass_rate"].sli_query


def test_slo_catalogue_is_json_safe() -> None:
    import json

    cat = slo_catalogue()
    encoded = json.dumps(cat)
    assert "api_availability" in encoded
    first = cat["slos"][0]  # type: ignore[index]
    assert "objective" in first
    assert "burn_windows" in first
