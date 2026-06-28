"""Tests for the smoke-test gate (offline)."""

from __future__ import annotations

import pytest

from deploy.orchestrator.smoke import (
    ScriptedSmokeCheck,
    SmokeCheck,
    SmokeGate,
    SmokeOutcome,
)


def _check(name: str, passed: bool, required: bool = True) -> SmokeCheck:
    return SmokeCheck(
        name=name,
        run=ScriptedSmokeCheck(SmokeOutcome(passed=passed)),
        required=required,
    )


async def test_all_pass() -> None:
    gate = SmokeGate(checks=[_check("health", True), _check("ready", True)])
    report = await gate.run("slot-1")
    assert report.passed is True
    assert report.failures == []


async def test_required_failure_blocks_and_short_circuits() -> None:
    third = ScriptedSmokeCheck(SmokeOutcome.ok())
    gate = SmokeGate(
        checks=[_check("health", True), _check("ready", False), SmokeCheck("after", third)],
        short_circuit=True,
    )
    report = await gate.run("slot-1")
    assert report.passed is False
    assert [r.name for r in report.blocking_failures] == ["ready"]
    # short-circuit means the third check never ran.
    assert third.calls == []


async def test_non_required_failure_is_advisory() -> None:
    gate = SmokeGate(
        checks=[_check("health", True), _check("preflight", False, required=False)],
        short_circuit=True,
    )
    report = await gate.run("slot-1")
    assert report.passed is True  # advisory failure does not block
    assert len(report.failures) == 1
    assert report.blocking_failures == []


async def test_no_short_circuit_runs_all() -> None:
    last = ScriptedSmokeCheck(SmokeOutcome.ok())
    gate = SmokeGate(
        checks=[_check("a", False), SmokeCheck("b", last)],
        short_circuit=False,
    )
    report = await gate.run("slot-1")
    assert report.passed is False
    assert last.calls == ["slot-1"]  # ran despite the earlier failure


async def test_throwing_check_is_a_failure_not_a_crash() -> None:
    async def boom(_target: str) -> SmokeOutcome:
        raise RuntimeError("kaboom")

    gate = SmokeGate(checks=[SmokeCheck("boom", boom)])
    report = await gate.run("slot-1")
    assert report.passed is False
    assert "kaboom" in report.failures[0].outcome.detail


def test_gate_requires_checks() -> None:
    with pytest.raises(ValueError):
        SmokeGate(checks=[])


def test_gate_rejects_duplicate_names() -> None:
    with pytest.raises(ValueError):
        SmokeGate(checks=[_check("x", True), _check("x", True)])


async def test_summary_string() -> None:
    gate = SmokeGate(checks=[_check("a", True), _check("b", False)], short_circuit=False)
    report = await gate.run("green")
    assert report.summary() == "smoke green: 1/2 passed"
