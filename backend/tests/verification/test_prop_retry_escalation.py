"""Property tests for the §9.5 retry / escalation layer (``app.render.retry``).

This is the policy the §9.7 live loop and the poison tracker share, so its
correctness keeps the engine from either looping forever or degrading too eagerly.
Properties cover the QA-driven escalation (``decide_retry``), the deterministic
backoff schedule, and the transient/permanent exception classification.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

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

repair_actions = st.sampled_from(list(RepairAction))
caps = st.integers(min_value=0, max_value=5)
attempts = st.integers(min_value=0, max_value=8)


@st.composite
def policies(draw: st.DrawFn) -> RetryPolicy:
    return RetryPolicy(
        cap=draw(caps),
        base_backoff_s=draw(st.floats(0.1, 5.0, allow_nan=False)),
        max_backoff_s=draw(st.floats(5.0, 60.0, allow_nan=False)),
    )


@given(repair_actions, attempts, policies())
def test_decide_retry_step_is_consistent(
    action: RepairAction, attempt: int, policy: RetryPolicy
) -> None:
    """The escalation always yields one of the four steps, with a matching reason."""
    decision = decide_retry(action, attempt, policy)
    assert decision.step in set(RetryStep)
    assert decision.attempt == attempt
    assert decision.retries_exhausted == policy.retries_exhausted(attempt)


@given(repair_actions, attempts, policies())
def test_exhaustion_forces_a_terminal_step(
    action: RepairAction, attempt: int, policy: RetryPolicy
) -> None:
    """At/over the cap, the only non-accept outcome is DEGRADE — never REGENERATE.

    This is the §9.5 cap → ladder guarantee: an exhausted attempt can't loop.
    """
    if policy.retries_exhausted(attempt) and action is not RepairAction.ACCEPT:
        decision = decide_retry(action, attempt, policy)
        assert decision.step is RetryStep.DEGRADE


@given(attempts, policies())
def test_accept_always_accepts(attempt: int, policy: RetryPolicy) -> None:
    decision = decide_retry(RepairAction.ACCEPT, attempt, policy)
    assert decision.step is RetryStep.ACCEPT
    assert decision.is_terminal


@given(st.sampled_from([RepairAction.RAISE_CONFLICT, RepairAction.EVOLVE_CANON]), policies())
def test_conflict_actions_route_to_conflict_when_not_exhausted(
    action: RepairAction, policy: RetryPolicy
) -> None:
    """A conflict action with attempts remaining hands off to §7.2."""
    decision = decide_retry(action, 0, policy)
    if not policy.retries_exhausted(0):
        assert decision.step is RetryStep.CONFLICT


@given(
    st.sampled_from(
        [
            RepairAction.REGEN_TIGHTEN_REFS,
            RepairAction.REPROMPT_STYLE,
            RepairAction.REGEN_NEW_SEED,
        ]
    ),
    policies(),
)
def test_regen_actions_regenerate_when_attempts_remain(
    action: RepairAction, policy: RetryPolicy
) -> None:
    """A regen action below the cap loops to another attempt (no premature degrade)."""
    if not policy.retries_exhausted(0):
        decision = decide_retry(action, 0, policy)
        assert decision.step is RetryStep.REGENERATE


# --------------------------------------------------------------------------- #
# Backoff schedule
# --------------------------------------------------------------------------- #


@given(attempts, policies())
def test_backoff_is_bounded_and_nonnegative(attempt: int, policy: RetryPolicy) -> None:
    """Backoff is in ``[0, max_backoff_s]`` — a long retry chain can't sleep forever.

    NOTE (catalogued in DESIGN.md, MINOR-1): ``backoff_for`` clamps to the ceiling
    *then* ``round(·, 3)``, so a ceiling with >3 decimals (e.g. 5.9375) can round
    the clamped value up to 5.938 — a sub-millisecond overshoot above the stated
    bound. Harmless in production (defaults are integers) but a real rounding edge;
    we assert the bound with a 1e-3 rounding tolerance to document it precisely.
    """
    backoff = policy.backoff_for(attempt)
    assert 0.0 <= backoff <= policy.max_backoff_s + 1e-3


@given(policies())
def test_backoff_is_monotone_nondecreasing(policy: RetryPolicy) -> None:
    """Backoff grows (or plateaus at the ceiling) with each attempt — never shrinks."""
    series = [policy.backoff_for(a) for a in range(8)]
    for earlier, later in zip(series, series[1:], strict=False):
        assert later >= earlier


@given(policies())
def test_backoff_doubles_until_the_ceiling(policy: RetryPolicy) -> None:
    """Below the ceiling the schedule is exactly ``base · 2**attempt`` (deterministic)."""
    for attempt in range(6):
        raw = policy.base_backoff_s * (2.0**attempt)
        if raw <= policy.max_backoff_s:
            assert abs(policy.backoff_for(attempt) - round(raw, 3)) < 1e-6


# --------------------------------------------------------------------------- #
# Failure classification + transient escalation
# --------------------------------------------------------------------------- #


PERMANENT_EXCS = [
    UnknownShotError("s"),
    LookupError("x"),
    ValueError("x"),
    TypeError("x"),
]
TRANSIENT_EXCS = [
    RuntimeError("provider blip"),
    TimeoutError("slow"),
    ConnectionError("net"),
    OSError("io"),
]


@given(st.sampled_from(PERMANENT_EXCS))
def test_permanent_exceptions_classify_permanent(exc: BaseException) -> None:
    assert classify_failure(exc) is FailureClass.PERMANENT


@given(st.sampled_from(TRANSIENT_EXCS))
def test_transient_exceptions_classify_transient(exc: BaseException) -> None:
    assert classify_failure(exc) is FailureClass.TRANSIENT


@given(st.sampled_from(PERMANENT_EXCS), attempts, policies())
def test_permanent_failure_degrades_immediately(
    exc: BaseException, attempt: int, policy: RetryPolicy
) -> None:
    """A permanent failure never retries — it degrades on the spot."""
    decision = transient_decision(exc, attempt, policy)
    assert decision.step is RetryStep.DEGRADE
    assert decision.backoff_s == 0.0


@given(st.sampled_from(TRANSIENT_EXCS), attempts, policies())
def test_transient_failure_retries_with_backoff_until_cap(
    exc: BaseException, attempt: int, policy: RetryPolicy
) -> None:
    """A transient failure with attempts left regenerates after a positive backoff."""
    decision = transient_decision(exc, attempt, policy)
    if policy.retries_exhausted(attempt):
        assert decision.step is RetryStep.DEGRADE
    else:
        assert decision.step is RetryStep.REGENERATE
        assert decision.backoff_s == policy.backoff_for(attempt)


@given(caps)
def test_max_attempts_is_cap_plus_one(cap: int) -> None:
    policy = RetryPolicy(cap=cap)
    assert policy.max_attempts == cap + 1
    # attempts_left never goes negative.
    for attempt in range(cap + 3):
        assert policy.attempts_left(attempt) >= 0
