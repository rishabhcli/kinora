"""Deterministic tests for SLIs, error budgets, and multi-window burn alerts."""

from __future__ import annotations

import pytest

from app.slo.objectives import (
    AlertSeverity,
    BudgetState,
    LatencyObjective,
    Objective,
    burn_rate,
    default_burn_policy,
)
from app.slo.sli import (
    DEFAULT_SLIS,
    SLIDefinition,
    SLIType,
    compute_latency_sli,
    compute_ratio_sli,
)
from app.slo.windows import CounterStream, SampleStream

RATIO = SLIDefinition("avail", SLIType.RATIO_GOOD, "api.avail")
LAT = SLIDefinition("p95", SLIType.LATENCY_P95, "render.ms", unit="ms")


# --- SLI computation ------------------------------------------------------- #


def test_compute_ratio_sli() -> None:
    s = CounterStream(horizon_s=100.0)
    for i in range(100):
        s.record(good=i < 99, now=float(i))  # 99 good, 1 bad => 0.99
    val = compute_ratio_sli(RATIO, s.window(now=99.0, window_s=100.0), window_s=100.0)
    assert val.value == 0.99
    assert val.sample_count == 100
    assert not val.empty


def test_compute_latency_sli() -> None:
    s = SampleStream(horizon_s=1000.0)
    for i in range(1, 101):
        s.record(float(i), now=float(i))
    val = compute_latency_sli(LAT, s.window(now=100.0, window_s=1000.0), window_s=1000.0)
    assert val.value == 95.0
    assert val.definition.unit == "ms"


# --- error budget ---------------------------------------------------------- #


def test_error_budget_and_objective_guards() -> None:
    obj = Objective("avail", RATIO, target=0.995)
    assert obj.error_budget == pytest.approx(0.005)
    with pytest.raises(ValueError):
        Objective("bad", RATIO, target=1.5)
    with pytest.raises(ValueError):
        Objective("wrong-sli", LAT, target=0.99)


def test_budget_state_half_consumed() -> None:
    obj = Objective("avail", RATIO, target=0.99)  # budget = 0.01
    # achieved 0.995 good => failure 0.005 => half the 0.01 budget spent.
    st = BudgetState(objective=obj, good_ratio=0.995, sample_count=1000)
    assert st.failure_ratio == pytest.approx(0.005)
    assert st.consumed == pytest.approx(0.5)
    assert st.remaining_fraction == pytest.approx(0.5)
    assert st.is_exhausted is False
    assert st.met is True


def test_budget_state_exhausted_and_blown() -> None:
    obj = Objective("avail", RATIO, target=0.99)  # budget = 0.01
    st = BudgetState(objective=obj, good_ratio=0.97, sample_count=1000)  # failure 0.03
    assert st.consumed == pytest.approx(3.0)
    assert st.remaining_fraction == pytest.approx(-2.0)
    assert st.is_exhausted is True
    assert st.met is False


def test_budget_state_perfect_target_no_budget() -> None:
    obj = Objective("perfect", RATIO, target=1.0)  # zero budget
    ok = BudgetState(objective=obj, good_ratio=1.0, sample_count=10)
    assert ok.consumed == 0.0
    assert ok.is_exhausted is False
    bad = BudgetState(objective=obj, good_ratio=0.999, sample_count=10)
    assert bad.consumed == float("inf")
    assert bad.is_exhausted is True


# --- burn rate ------------------------------------------------------------- #


def test_burn_rate_math() -> None:
    # budget 0.005 (99.5%); a 0.072 failure rate => 14.4x burn (the SRE page).
    assert burn_rate(0.072, 0.005) == pytest.approx(14.4)
    assert burn_rate(0.0, 0.005) == 0.0
    assert burn_rate(0.0, 0.0) == 0.0
    assert burn_rate(0.01, 0.0) == float("inf")


# --- multi-window burn policy ---------------------------------------------- #


def test_fast_burn_page_fires_when_both_windows_agree() -> None:
    policy = default_burn_policy()
    obj = Objective("avail", RATIO, target=0.995)  # budget 0.005
    # 0.072 failure in both the 1h and 5m windows => 14.4x => PAGE.
    failure = {3600.0: 0.072, 300.0: 0.072, 3 * 24 * 3600.0: 0.0, 6 * 3600.0: 0.0}
    alert = policy.evaluate(obj, failure_by_window=failure)
    assert alert.severity is AlertSeverity.PAGE
    assert alert.firing is True
    assert alert.long_burn == pytest.approx(14.4)


def test_fast_burn_does_not_fire_on_spike_only_in_short_window() -> None:
    # Short window hot but long (1h) window cool => no page (the SRE noise filter).
    policy = default_burn_policy()
    obj = Objective("avail", RATIO, target=0.995)
    failure = {3600.0: 0.0, 300.0: 0.072, 3 * 24 * 3600.0: 0.0, 6 * 3600.0: 0.0}
    alert = policy.evaluate(obj, failure_by_window=failure)
    assert alert.severity is AlertSeverity.NONE
    assert alert.firing is False


def test_slow_burn_ticket_fires() -> None:
    policy = default_burn_policy()
    obj = Objective("avail", RATIO, target=0.995)  # budget 0.005
    # ~1.0x burn over 3d + 6h, but below the 14.4x page threshold => TICKET.
    failure = {
        3600.0: 0.0,
        300.0: 0.0,
        3 * 24 * 3600.0: 0.006,  # 1.2x
        6 * 3600.0: 0.006,
    }
    alert = policy.evaluate(obj, failure_by_window=failure)
    assert alert.severity is AlertSeverity.TICKET


def test_page_preempts_ticket() -> None:
    policy = default_burn_policy()
    obj = Objective("avail", RATIO, target=0.995)
    failure = {
        3600.0: 0.072,  # page
        300.0: 0.072,
        3 * 24 * 3600.0: 0.006,  # ticket
        6 * 3600.0: 0.006,
    }
    alert = policy.evaluate(obj, failure_by_window=failure)
    assert alert.severity is AlertSeverity.PAGE  # worst wins


def test_alert_clears_when_healthy() -> None:
    policy = default_burn_policy()
    obj = Objective("avail", RATIO, target=0.995)
    failure = {3600.0: 0.0, 300.0: 0.0, 3 * 24 * 3600.0: 0.0, 6 * 3600.0: 0.0}
    alert = policy.evaluate(obj, failure_by_window=failure)
    assert alert.severity is AlertSeverity.NONE
    assert alert.firing is False


def test_policy_window_lengths_are_distinct_sorted() -> None:
    lengths = default_burn_policy().window_lengths
    assert lengths == (300.0, 3600.0, 6 * 3600.0, 3 * 24 * 3600.0)


# --- latency objective ----------------------------------------------------- #


def test_latency_objective_met_and_missed() -> None:
    obj = LatencyObjective("render-p95", LAT, target_ms=8000.0)

    fast = SampleStream(horizon_s=1000.0)
    for v in (1000.0, 2000.0, 3000.0):
        fast.record(v, now=1.0)
    good = obj.evaluate(
        compute_latency_sli(LAT, fast.window(now=1.0, window_s=1000.0), window_s=1000.0)
    )
    assert good.met is True
    assert good.margin_ms > 0

    slow = SampleStream(horizon_s=1000.0)
    for _ in range(5):
        slow.record(9000.0, now=1.0)  # over the 8000ms budget
    miss = obj.evaluate(
        compute_latency_sli(LAT, slow.window(now=1.0, window_s=1000.0), window_s=1000.0)
    )
    assert miss.met is False
    assert miss.margin_ms < 0


def test_default_slis_cover_product_objectives() -> None:
    names = {d.name for d in DEFAULT_SLIS}
    assert {
        "read_underrun_free",
        "shot_success_rate",
        "api_availability",
        "render_latency_p95",
        "intent_latency_p99",
    } <= names
