"""The deterministic §9.5 retry / repair escalation policy.

Asserts the escalation mirrors the pipeline's live-loop branch order, the retry
cap → degrade, deterministic backoff, and transient-vs-permanent classification.
Pure — no DB/network/ffmpeg.
"""

from __future__ import annotations

import pytest

from app.agents.contracts import RepairAction
from app.render.pipeline import UnknownShotError
from app.render.retry import (
    FailureClass,
    RetryPolicy,
    RetryStep,
    classify_failure,
    decide_retry,
    transient_decision,
)


def test_policy_attempt_accounting() -> None:
    policy = RetryPolicy(cap=2)
    assert policy.max_attempts == 3
    assert policy.attempts_left(0) == 2
    assert policy.attempts_left(2) == 0
    assert not policy.retries_exhausted(0)
    assert not policy.retries_exhausted(1)
    assert policy.retries_exhausted(2)


def test_backoff_is_deterministic_exponential_and_capped() -> None:
    policy = RetryPolicy(cap=10, base_backoff_s=1.0, max_backoff_s=8.0)
    assert policy.backoff_for(0) == 1.0
    assert policy.backoff_for(1) == 2.0
    assert policy.backoff_for(2) == 4.0
    assert policy.backoff_for(3) == 8.0
    assert policy.backoff_for(4) == 8.0  # capped
    # Reproducible across calls (no jitter).
    assert policy.backoff_for(2) == policy.backoff_for(2)


def test_accept_routes_to_accept() -> None:
    d = decide_retry(RepairAction.ACCEPT, 0, RetryPolicy())
    assert d.step is RetryStep.ACCEPT
    assert d.is_terminal


@pytest.mark.parametrize(
    "action",
    [RepairAction.REGEN_TIGHTEN_REFS, RepairAction.REPROMPT_STYLE, RepairAction.REGEN_NEW_SEED],
)
def test_regen_actions_regenerate_when_attempts_left(action: RepairAction) -> None:
    d = decide_retry(action, 0, RetryPolicy(cap=2))
    assert d.step is RetryStep.REGENERATE
    assert not d.is_terminal
    assert d.action is action


@pytest.mark.parametrize(
    "action", [RepairAction.RAISE_CONFLICT, RepairAction.EVOLVE_CANON]
)
def test_conflict_actions_route_to_conflict(action: RepairAction) -> None:
    d = decide_retry(action, 0, RetryPolicy())
    assert d.step is RetryStep.CONFLICT
    assert not d.is_terminal


def test_explicit_degrade_action_degrades() -> None:
    d = decide_retry(RepairAction.DEGRADE, 0, RetryPolicy())
    assert d.step is RetryStep.DEGRADE
    assert d.is_terminal
    assert d.reason == "degrade"


def test_retry_cap_forces_degrade_even_for_a_regen_action() -> None:
    # On the final attempt, a would-be regen still degrades (§9.5 cap → ladder).
    d = decide_retry(RepairAction.REGEN_TIGHTEN_REFS, 2, RetryPolicy(cap=2))
    assert d.step is RetryStep.DEGRADE
    assert d.retries_exhausted
    assert d.reason == "retries_exhausted"


def test_cap_also_forces_degrade_for_a_conflict_action() -> None:
    # The pipeline checks DEGRADE/exhausted *before* conflict routing — mirror it.
    d = decide_retry(RepairAction.RAISE_CONFLICT, 2, RetryPolicy(cap=2))
    assert d.step is RetryStep.DEGRADE


# --------------------------------------------------------------------------- #
# Exception classification + transient escalation
# --------------------------------------------------------------------------- #


def test_classify_permanent_failures() -> None:
    assert classify_failure(UnknownShotError("no shot")) is FailureClass.PERMANENT
    assert classify_failure(ValueError("bad")) is FailureClass.PERMANENT
    assert classify_failure(KeyError("missing")) is FailureClass.PERMANENT  # LookupError


def test_classify_transient_default() -> None:
    assert classify_failure(RuntimeError("provider blip")) is FailureClass.TRANSIENT
    assert classify_failure(TimeoutError("slow")) is FailureClass.TRANSIENT


def test_transient_failure_retries_with_backoff() -> None:
    d = transient_decision(RuntimeError("blip"), 0, RetryPolicy(cap=2, base_backoff_s=2.0))
    assert d.step is RetryStep.REGENERATE
    assert d.backoff_s == 2.0
    assert d.reason == "RuntimeError"


def test_permanent_failure_degrades_immediately() -> None:
    d = transient_decision(UnknownShotError("gone"), 0, RetryPolicy(cap=2))
    assert d.step is RetryStep.DEGRADE
    assert d.backoff_s == 0.0


def test_transient_failure_degrades_at_cap() -> None:
    d = transient_decision(RuntimeError("blip"), 2, RetryPolicy(cap=2))
    assert d.step is RetryStep.DEGRADE
    assert d.retries_exhausted
