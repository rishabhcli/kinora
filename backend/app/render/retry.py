"""Deterministic retry / repair escalation for the §9.5 self-correcting loop.

The Critic's :func:`app.agents.critic.decide_qa` already decides *which* repair a
failed clip needs (tighten refs / reprompt style / new seed / raise conflict /
evolve canon / degrade). This module owns the *escalation* layer on top — the
policy the §9.7 pipeline's live loop currently inlines:

* a **retry cap** (default 2 from settings, §9.5): after the cap, any further
  failure drops to the degradation ladder rather than looping forever;
* the routing of a :class:`app.agents.contracts.RepairAction` to a next
  :class:`RetryStep` — ``REGENERATE`` (re-design / re-seed and try again),
  ``CONFLICT`` (hand to §7.2), ``DEGRADE`` (step down the ladder), or ``ACCEPT``;
* a **deterministic backoff** schedule for *transient infrastructure* failures
  (a provider hiccup that isn't a QA fail) so the worker's re-claim path and the
  poison tracker share one schedule;
* a classification of exceptions into permanent (never retry — straight to
  degrade) vs transient.

Extracting this makes the escalation unit-testable in isolation and lets the DAG
scheduler + the poison tracker reuse exactly the pipeline's policy — they can
never drift. Pure functions; no DB/network/ffmpeg.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.agents.contracts import RepairAction
from app.core.logging import get_logger

logger = get_logger("app.render.retry")


class RetryStep(StrEnum):
    """What the escalation layer does next with a failed attempt (§9.5/§9.7)."""

    ACCEPT = "accept"
    #: Re-design / re-seed and render again (a normal QA-fail repair).
    REGENERATE = "regenerate"
    #: A timeline contradiction → hand to the §7.2 conflict flow.
    CONFLICT = "conflict"
    #: Step down the §12.4 ladder (cap hit, explicit degrade, or permanent error).
    DEGRADE = "degrade"


#: The §9.5 repair actions that mean "regenerate and try again".
_REGEN_ACTIONS: frozenset[RepairAction] = frozenset(
    {
        RepairAction.REGEN_TIGHTEN_REFS,
        RepairAction.REPROMPT_STYLE,
        RepairAction.REGEN_NEW_SEED,
    }
)
#: The actions that hand off to the §7.2 conflict flow.
_CONFLICT_ACTIONS: frozenset[RepairAction] = frozenset(
    {RepairAction.RAISE_CONFLICT, RepairAction.EVOLVE_CANON}
)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """The §9.5 escalation policy (defaults mirror ``Settings.retry_cap``).

    Attributes:
        cap: max repair *retries* — total attempts is ``cap + 1`` (the initial
            render plus ``cap`` repairs), matching the pipeline's loop range.
        base_backoff_s: first transient-failure backoff (doubles each retry).
        max_backoff_s: backoff ceiling so a long retry chain can't sleep forever.
    """

    cap: int = 2
    base_backoff_s: float = 1.0
    max_backoff_s: float = 30.0

    @property
    def max_attempts(self) -> int:
        """Total render attempts allowed (initial + ``cap`` repairs)."""
        return self.cap + 1

    def attempts_left(self, attempt: int) -> int:
        """Repairs still allowed after a zero-based ``attempt`` index."""
        return max(self.cap - attempt, 0)

    def retries_exhausted(self, attempt: int) -> bool:
        """True on the final allowed attempt (the pipeline's ``retries_exhausted``)."""
        return attempt >= self.cap

    def backoff_for(self, attempt: int) -> float:
        """Deterministic exponential backoff for a transient retry (capped).

        ``attempt`` is zero-based; backoff is ``base * 2**attempt`` clamped to
        ``max_backoff_s``. Deterministic (no jitter) so tests + the simulator are
        reproducible and the poison tracker can reason about total wait time.
        """
        raw = self.base_backoff_s * (2.0**max(attempt, 0))
        return round(min(raw, self.max_backoff_s), 3)


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """The escalation verdict for one failed attempt."""

    step: RetryStep
    attempt: int
    retries_exhausted: bool
    backoff_s: float
    action: RepairAction | None = None
    reason: str = ""

    @property
    def is_terminal(self) -> bool:
        """True when this decision ends the loop (accept, conflict-surface, degrade)."""
        return self.step in (RetryStep.ACCEPT, RetryStep.DEGRADE)


def decide_retry(action: RepairAction, attempt: int, policy: RetryPolicy) -> RetryDecision:
    """Escalate a §9.5 :class:`RepairAction` at a zero-based ``attempt`` (pure).

    Mirrors the pipeline's live-loop branch order exactly:
      * ``ACCEPT`` → accept;
      * ``DEGRADE`` **or** retries exhausted → degrade (the §9.5 cap → ladder);
      * conflict actions → hand to §7.2;
      * a regen action with attempts left → regenerate (re-design/re-seed).
    """
    exhausted = policy.retries_exhausted(attempt)
    if action is RepairAction.ACCEPT:
        return RetryDecision(
            step=RetryStep.ACCEPT,
            attempt=attempt,
            retries_exhausted=exhausted,
            backoff_s=0.0,
            action=action,
            reason="qa_pass",
        )
    if action is RepairAction.DEGRADE or exhausted:
        return RetryDecision(
            step=RetryStep.DEGRADE,
            attempt=attempt,
            retries_exhausted=exhausted,
            backoff_s=0.0,
            action=action,
            reason="retries_exhausted" if exhausted else "degrade",
        )
    if action in _CONFLICT_ACTIONS:
        return RetryDecision(
            step=RetryStep.CONFLICT,
            attempt=attempt,
            retries_exhausted=exhausted,
            backoff_s=0.0,
            action=action,
            reason=action.value,
        )
    # A regen action with attempts remaining.
    decision = RetryDecision(
        step=RetryStep.REGENERATE,
        attempt=attempt,
        retries_exhausted=exhausted,
        backoff_s=0.0,
        action=action,
        reason=action.value,
    )
    logger.info("retry.regenerate", attempt=attempt, action=action.value)
    return decision


# --------------------------------------------------------------------------- #
# Transient vs permanent classification (infra failures, not QA fails)
# --------------------------------------------------------------------------- #


class FailureClass(StrEnum):
    """How a raised exception should be handled by the escalation layer."""

    #: A QA-driven repair routed through ``decide_retry`` (not an exception).
    REPAIR = "repair"
    #: A transient infra/provider failure — back off + retry up to the cap.
    TRANSIENT = "transient"
    #: A permanent failure — never retry; degrade (or DLQ) immediately.
    PERMANENT = "permanent"


def classify_failure(exc: BaseException) -> FailureClass:
    """Classify a raised render exception (mirrors the worker's ``_PERMANENT``).

    ``UnknownShotError``/``LookupError``/``ValueError`` can never succeed on retry
    → ``PERMANENT``. Provider/IO/timeout errors are ``TRANSIENT``. The default for
    an unknown exception is conservative (``TRANSIENT``) so a one-off blip retries
    rather than instantly degrading — the poison tracker catches a true crash-loop.
    """
    from app.render.pipeline import UnknownShotError

    if isinstance(exc, UnknownShotError | LookupError | ValueError | TypeError):
        return FailureClass.PERMANENT
    return FailureClass.TRANSIENT


def transient_decision(exc: BaseException, attempt: int, policy: RetryPolicy) -> RetryDecision:
    """Escalate a *raised* (non-QA) failure: retry with backoff, else degrade.

    A permanent failure (or the cap) degrades immediately; a transient one with
    attempts left schedules a deterministic backoff and regenerates.
    """
    klass = classify_failure(exc)
    exhausted = policy.retries_exhausted(attempt)
    if klass is FailureClass.PERMANENT or exhausted:
        return RetryDecision(
            step=RetryStep.DEGRADE,
            attempt=attempt,
            retries_exhausted=exhausted,
            backoff_s=0.0,
            reason=type(exc).__name__,
        )
    return RetryDecision(
        step=RetryStep.REGENERATE,
        attempt=attempt,
        retries_exhausted=exhausted,
        backoff_s=policy.backoff_for(attempt),
        reason=type(exc).__name__,
    )


__all__ = [
    "FailureClass",
    "RetryDecision",
    "RetryPolicy",
    "RetryStep",
    "classify_failure",
    "decide_retry",
    "transient_decision",
]
