"""Unit tests for runbooks-as-code (app.reliability.runbook)."""

from __future__ import annotations

from app.reliability.runbook import (
    Runbook,
    RunbookStep,
    Severity,
    buffer_stall_runbook,
    dlq_backlog_runbook,
    provider_rate_limit_runbook,
    queue_backpressure_runbook,
    redis_partition_runbook,
    standard_runbooks,
    triggered_runbooks,
)


def test_runbook_not_triggered_yields_empty_plan() -> None:
    rb = dlq_backlog_runbook(threshold=5)
    plan = rb.plan({"dlq_len": 0})
    assert plan.triggered is False
    assert plan.steps == ()


def test_runbook_triggered_yields_ordered_steps() -> None:
    rb = dlq_backlog_runbook(threshold=5)
    plan = rb.plan({"dlq_len": 10, "provider_error_rate": 0.0})
    assert plan.triggered is True
    titles = [s.title for s in plan.steps]
    # The provider-correlation step has a check that does NOT apply here.
    assert "Check for a provider-wide failure" not in titles
    assert titles[0] == "Confirm the pipeline is not blocked"


def test_step_check_gates_inclusion() -> None:
    rb = dlq_backlog_runbook(threshold=5)
    # High provider error rate => the provider-correlation step is included.
    plan = rb.plan({"dlq_len": 10, "provider_error_rate": 0.5})
    titles = [s.title for s in plan.steps]
    assert "Check for a provider-wide failure" in titles


def test_automatable_vs_manual_split() -> None:
    rb = redis_partition_runbook()
    plan = rb.plan({"redis_unreachable": True})
    assert plan.triggered is True
    auto_titles = {s.title for s in plan.automatable}
    manual_titles = {s.title for s in plan.manual}
    assert "Reap orphaned leases after recovery" in auto_titles
    assert "Restore Redis connectivity" in manual_titles
    # No overlap.
    assert auto_titles.isdisjoint(manual_titles)


def test_provider_rate_limit_triggers_on_flag_or_rate() -> None:
    rb = provider_rate_limit_runbook()
    assert rb.plan({"provider_throttled": True}).triggered is True
    assert rb.plan({"provider_error_rate": 0.3}).triggered is True
    assert rb.plan({"provider_error_rate": 0.05}).triggered is False


def test_buffer_stall_triggers_below_low_watermark() -> None:
    rb = buffer_stall_runbook(low_watermark_s=25.0)
    assert rb.plan({"committed_seconds_ahead": 10.0}).triggered is True
    assert rb.plan({"committed_seconds_ahead": 40.0}).triggered is False
    # Missing signal defaults to L (safe: not triggered).
    assert rb.plan({}).triggered is False


def test_buffer_stall_capacity_step_gated_on_utilisation() -> None:
    rb = buffer_stall_runbook()
    saturated = rb.plan({"committed_seconds_ahead": 5.0, "render_utilisation": 0.95})
    healthy = rb.plan({"committed_seconds_ahead": 5.0, "render_utilisation": 0.4})
    sat_titles = {s.title for s in saturated.steps}
    healthy_titles = {s.title for s in healthy.steps}
    assert "Check render-worker capacity" in sat_titles
    assert "Check render-worker capacity" not in healthy_titles


def test_queue_backpressure_is_info_severity() -> None:
    rb = queue_backpressure_runbook(depth_threshold=64)
    plan = rb.plan({"queue_depth": 100})
    assert plan.severity is Severity.INFO
    assert plan.triggered is True


def test_standard_registry_has_all_incidents() -> None:
    registry = standard_runbooks()
    assert {
        "dlq_backlog",
        "provider_rate_limit",
        "buffer_stall",
        "budget_low",
        "queue_backpressure",
        "redis_partition",
    } <= set(registry)
    # Every runbook has a non-empty summary + at least one step.
    for rb in registry.values():
        assert rb.summary
        assert len(rb.steps) >= 1


def test_triggered_runbooks_sorted_by_severity() -> None:
    # A signals snapshot that lights up a PAGE (buffer stall), a TICKET (budget),
    # and an INFO (backpressure) incident at once.
    signals = {
        "committed_seconds_ahead": 5.0,  # PAGE: buffer_stall
        "budget_remaining_s": 50.0,  # TICKET: budget_low
        "queue_depth": 80,  # INFO: queue_backpressure
        "render_utilisation": 0.9,
    }
    plans = triggered_runbooks(signals)
    severities = [p.severity for p in plans]
    # Page first, info last.
    assert severities[0] is Severity.PAGE
    assert severities[-1] is Severity.INFO
    names = {p.runbook for p in plans}
    assert {"buffer_stall", "budget_low", "queue_backpressure"} <= names


def test_triggered_runbooks_empty_when_healthy() -> None:
    healthy = {
        "dlq_len": 0,
        "provider_error_rate": 0.0,
        "committed_seconds_ahead": 60.0,
        "budget_remaining_s": 1500.0,
        "queue_depth": 1,
        "redis_unreachable": False,
    }
    assert triggered_runbooks(healthy) == []


def test_plan_render_text() -> None:
    rb = redis_partition_runbook()
    plan = rb.plan({"redis_unreachable": True})
    text = plan.render_text()
    assert "redis_partition" in text
    assert "page" in text
    assert "[auto]" in text or "[manual]" in text


def test_custom_runbook() -> None:
    rb = Runbook(
        name="custom",
        severity=Severity.TICKET,
        summary="A custom incident.",
        trigger=lambda s: bool(s.get("fire")),
        steps=(RunbookStep(title="Do the thing", action="…", automation=True),),
    )
    assert rb.plan({"fire": False}).triggered is False
    plan = rb.plan({"fire": True})
    assert plan.triggered is True
    assert plan.automatable[0].title == "Do the thing"
