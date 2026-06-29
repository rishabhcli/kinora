"""Distributed-transaction seam: two-phase commit + saga, over shard participants.

A write that touches more than one shard cannot lean on a single Postgres
transaction. Two patterns cover the field, and this module provides both behind
a common :class:`Participant` protocol so a caller picks the right consistency /
availability trade-off per operation:

* **Two-phase commit (2PC).** A coordinator drives every participant through
  ``prepare`` → ``commit`` (or ``abort``). If *all* prepare votes are YES the
  transaction commits everywhere; a single NO (or a prepare error) aborts
  everywhere. This is the strong-consistency choice (atomic across shards) at the
  cost of a blocking window — a participant that voted YES holds its prepared
  state until the coordinator's decision. Postgres backs this with
  ``PREPARE TRANSACTION`` / ``COMMIT PREPARED``; here the participant abstraction
  lets us prove the *protocol* deterministically with fakes.

* **Saga.** A sequence of local transactions, each with a *compensating* action.
  Steps run forward; if a step fails the coordinator runs the compensations of
  the already-completed steps in reverse. This trades atomicity for
  availability + no blocking — the system is eventually consistent and never
  holds a cross-shard lock. It is the right tool for long or cross-service
  workflows (e.g. "reserve budget on shard A, enqueue render on shard B").

Both coordinators are *failure-aware and logged*: every protocol decision is
recorded so a crash mid-protocol can be reasoned about (the recovery of an
in-doubt 2PC transaction is a documented operational step, not silent). The
coordinators are async and pure-orchestration; participants own the I/O.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.core.logging import get_logger

logger = get_logger("app.datascale.sharding.transaction")


# --------------------------------------------------------------------------- #
# Two-phase commit
# --------------------------------------------------------------------------- #


class Vote(enum.Enum):
    """A participant's prepare-phase vote."""

    YES = "yes"
    NO = "no"


class TwoPCParticipant(Protocol):
    """One shard's view of a 2PC transaction.

    A real participant maps these to Postgres ``PREPARE TRANSACTION 'gid'`` /
    ``COMMIT PREPARED 'gid'`` / ``ROLLBACK PREPARED 'gid'`` on the shard's
    connection. ``prepare`` must be durable: after returning :attr:`Vote.YES` the
    participant guarantees it *can* commit even across a crash.
    """

    @property
    def shard_id(self) -> str: ...

    async def prepare(self, gid: str) -> Vote:
        """Do the work, make it durable, and vote YES/NO. Must not commit yet."""
        ...

    async def commit(self, gid: str) -> None:
        """Commit the prepared transaction (idempotent)."""
        ...

    async def abort(self, gid: str) -> None:
        """Roll back the prepared transaction (idempotent; safe if never prepared)."""
        ...


class TransactionOutcome(enum.Enum):
    """The terminal state of a coordinated transaction."""

    COMMITTED = "committed"
    ABORTED = "aborted"


class TwoPCError(RuntimeError):
    """Raised when 2PC aborts, carrying the reason + which participants prepared."""

    def __init__(self, reason: str, *, prepared: Sequence[str]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.prepared = tuple(prepared)


@dataclass(slots=True)
class TwoPCResult:
    """The result of a 2PC run (for logging / assertions)."""

    gid: str
    outcome: TransactionOutcome
    votes: dict[str, Vote] = field(default_factory=dict)
    committed: tuple[str, ...] = ()
    aborted: tuple[str, ...] = ()


@dataclass(slots=True)
class TwoPhaseCommitCoordinator:
    """Drives a set of participants through prepare → commit/abort atomically.

    The protocol:

    1. **Prepare** every participant. Collect votes. A prepare that *raises* is a
       NO vote (the participant is assumed not durable).
    2. **Decide.** All YES ⇒ commit; any NO/error ⇒ abort.
    3. **Commit / abort** every participant that prepared. Commit/abort are
       idempotent and retried best-effort; a participant that fails to commit
       after voting YES is logged as *in-doubt* — recovery (re-running
       ``commit_prepared``) is an operational step, never a silent data loss.

    The coordinator never blocks holding a lock itself; the blocking window is
    on the participants between their YES vote and the decision.
    """

    #: Best-effort retry count for the commit/abort phase per participant.
    finalize_attempts: int = 3

    async def run(
        self, gid: str, participants: Sequence[TwoPCParticipant]
    ) -> TwoPCResult:
        """Execute the full 2PC protocol; raise :class:`TwoPCError` on abort."""
        if not participants:
            raise ValueError("2PC needs at least one participant")
        votes: dict[str, Vote] = {}
        prepared: list[TwoPCParticipant] = []

        # Phase 1: prepare + vote.
        for participant in participants:
            vote = await self._prepare_one(gid, participant)
            votes[participant.shard_id] = vote
            if vote is Vote.YES:
                prepared.append(participant)
            else:
                # A NO vote ends phase 1 early; we abort what prepared.
                break

        decision_commit = all(v is Vote.YES for v in votes.values()) and len(votes) == len(
            participants
        )

        if decision_commit:
            logger.info("twopc.commit", gid=gid, participants=len(participants))
            committed = await self._finalize(gid, prepared, commit=True)
            return TwoPCResult(
                gid=gid,
                outcome=TransactionOutcome.COMMITTED,
                votes=votes,
                committed=tuple(committed),
            )

        logger.warning("twopc.abort", gid=gid, votes={k: v.value for k, v in votes.items()})
        await self._finalize(gid, prepared, commit=False)
        raise TwoPCError(
            f"2PC aborted for gid={gid!r}: votes={ {k: v.value for k, v in votes.items()} }",
            prepared=[p.shard_id for p in prepared],
        )

    async def _prepare_one(self, gid: str, participant: TwoPCParticipant) -> Vote:
        try:
            vote = await participant.prepare(gid)
        except Exception as exc:  # noqa: BLE001 - a prepare error is a NO vote
            logger.warning(
                "twopc.prepare_failed", gid=gid, shard=participant.shard_id, error=str(exc)
            )
            return Vote.NO
        return vote

    async def _finalize(
        self, gid: str, prepared: Sequence[TwoPCParticipant], *, commit: bool
    ) -> list[str]:
        """Commit-or-abort every prepared participant, idempotent + retried.

        A participant that cannot be finalized after ``finalize_attempts`` is
        logged as in-doubt and reported back via the result/exception so an
        operator (or a recovery sweep) can re-drive ``COMMIT/ROLLBACK PREPARED``.
        """
        finalized: list[str] = []
        for participant in prepared:
            ok = await self._finalize_one(gid, participant, commit=commit)
            if ok:
                finalized.append(participant.shard_id)
            else:
                logger.error(
                    "twopc.in_doubt",
                    gid=gid,
                    shard=participant.shard_id,
                    phase="commit" if commit else "abort",
                )
        return finalized

    async def _finalize_one(
        self, gid: str, participant: TwoPCParticipant, *, commit: bool
    ) -> bool:
        for attempt in range(1, self.finalize_attempts + 1):
            try:
                if commit:
                    await participant.commit(gid)
                else:
                    await participant.abort(gid)
                return True
            except Exception as exc:  # noqa: BLE001 - retried; reported if exhausted
                logger.warning(
                    "twopc.finalize_retry",
                    gid=gid,
                    shard=participant.shard_id,
                    attempt=attempt,
                    error=str(exc),
                )
        return False


# --------------------------------------------------------------------------- #
# Saga
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SagaStep:
    """One forward action + its compensation.

    ``action`` performs the step (a local transaction on one shard). ``compensate``
    semantically undoes a *completed* action; it must be idempotent and should
    itself be retry-safe, because the saga may re-run it during rollback. ``name``
    is for logging/tracing.
    """

    name: str
    action: Callable[[], Awaitable[object]]
    compensate: Callable[[], Awaitable[None]]


class SagaError(RuntimeError):
    """Raised when a saga fails; reports the failed step + compensation status."""

    def __init__(
        self,
        reason: str,
        *,
        failed_step: str,
        compensated: Sequence[str],
        compensation_failures: Sequence[str],
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.failed_step = failed_step
        self.compensated = tuple(compensated)
        self.compensation_failures = tuple(compensation_failures)


@dataclass(slots=True)
class SagaResult:
    """The result of a successful saga (the ordered step results)."""

    results: list[object] = field(default_factory=list)
    completed_steps: tuple[str, ...] = ()


@dataclass(slots=True)
class SagaCoordinator:
    """Runs steps forward; on failure, compensates completed steps in reverse.

    The guarantee is *semantic atomicity*: either every step's effect stands, or
    every completed step is compensated. It is **not** isolation — intermediate
    states are visible (the hallmark of a saga). A compensation that itself fails
    is logged and recorded; the saga still attempts the remaining compensations
    (one stuck undo must not strand the others) and surfaces the failures so an
    operator can finish the rollback.
    """

    #: Best-effort retry count per compensation.
    compensate_attempts: int = 3

    async def run(self, steps: Sequence[SagaStep]) -> SagaResult:
        """Execute the saga; raise :class:`SagaError` (after compensating) on failure."""
        if not steps:
            return SagaResult()
        completed: list[SagaStep] = []
        results: list[object] = []
        for step in steps:
            try:
                result = await step.action()
            except Exception as exc:  # noqa: BLE001 - triggers compensation
                logger.warning("saga.step_failed", step=step.name, error=str(exc))
                compensated, failures = await self._compensate(completed)
                raise SagaError(
                    f"saga step {step.name!r} failed: {exc}",
                    failed_step=step.name,
                    compensated=compensated,
                    compensation_failures=failures,
                ) from exc
            completed.append(step)
            results.append(result)
        logger.info("saga.committed", steps=len(steps))
        return SagaResult(results=results, completed_steps=tuple(s.name for s in steps))

    async def _compensate(self, completed: Sequence[SagaStep]) -> tuple[list[str], list[str]]:
        """Compensate completed steps in reverse order; return (done, failed)."""
        compensated: list[str] = []
        failures: list[str] = []
        for step in reversed(completed):
            if await self._compensate_one(step):
                compensated.append(step.name)
            else:
                failures.append(step.name)
        return compensated, failures

    async def _compensate_one(self, step: SagaStep) -> bool:
        for attempt in range(1, self.compensate_attempts + 1):
            try:
                await step.compensate()
                logger.info("saga.compensated", step=step.name)
                return True
            except Exception as exc:  # noqa: BLE001 - retried; reported if exhausted
                logger.warning(
                    "saga.compensate_retry", step=step.name, attempt=attempt, error=str(exc)
                )
        logger.error("saga.compensate_failed", step=step.name)
        return False


__all__ = [
    "SagaCoordinator",
    "SagaError",
    "SagaResult",
    "SagaStep",
    "TransactionOutcome",
    "TwoPCError",
    "TwoPCParticipant",
    "TwoPCResult",
    "TwoPhaseCommitCoordinator",
    "Vote",
]
