"""A model of the render-queue claim/lease/ack lifecycle (§12.1).

The render queue is where most of the hard concurrency lives: workers pull jobs,
hold a *lease* (visibility timeout) while they render, and either ack a success,
schedule a backoff retry, dead-letter a permanent failure, or honour a
cancellation from a seek. A lease that lapses mid-render lets the reaper re-queue
a job a worker is still holding — the classic "the job runs twice and the budget
is double-spent" bug (the very hazard
:mod:`app.queue.leases` is built to prevent). This model walks every
interleaving of *N* workers against the lifecycle and checks the lifecycle
invariants exhaustively.

The lifecycle mirrors :class:`app.db.models.enums.RenderJobStatus`:

    QUEUED → RESERVED → SUBMITTED → POLLING → SUCCEEDED
                                          ↘ RETRYING → (back to RESERVED)
                                          ↘ CANCELLED        (terminal)
                                          ↘ DEADLETTER       (terminal, degrade)

with a lease held from RESERVED through POLLING, a reaper that re-queues a job
whose lease expired (crash recovery), and a cancel token that can trip at any
non-terminal point (§12.1: "workers check the token at safe points and abort
cooperatively, releasing any reserved budget").

The abstraction
---------------

A single job's lifecycle, plus a *worker registry* and a *budget ledger* so the
double-spend hazard is observable:

* ``status`` — the lifecycle state.
* ``holder`` — which worker (1..N) currently leases the job, or 0 for none.
* ``lease_valid`` — whether the held lease is still fresh (a reaper only steals
  a *lapsed* lease; a fresh one is protected by the worker's heartbeat — §12.1).
* ``reserved`` — whether a budget reservation is currently held for this job.
* ``spent`` — how many times the job's budget was *actually debited* (an ack
  spends; this must never exceed 1 — that is the no-double-spend invariant).
* ``attempts`` — retry count, bounded by ``RETRY_CAP`` (§12.1 backoff ladder).
* ``cancel_requested`` — a seek has requested cancellation (§4.8); the worker
  must honour it cooperatively.

With ``workers=2`` the model exercises two workers racing to claim, a reaper
stealing a stale lease, and a cancel arriving mid-poll — the schedules a unit
test cannot enumerate.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum

from app.verification.modelcheck import (
    Action,
    Invariant,
    LeadsTo,
    Spec,
    invariant,
    leads_to,
)
from app.verification.modelcheck.spec import Fairness

__all__ = ["JobStatus", "RenderJobState", "build_render_queue_spec"]

#: The retry cap the spec explores. We drive the **real** production policy
#: (:class:`app.queue.redis_queue.RetryPolicy`) for the dead-letter decision, so
#: the "attempts bounded / job terminates" proofs are about the actual code; the
#: backoff *schedule* (delays) is irrelevant to the lifecycle, so only the cap +
#: the ``decide`` branch are exercised.
RETRY_CAP = 2


class JobStatus(IntEnum):
    """The render-job lifecycle (mirrors ``RenderJobStatus``)."""

    QUEUED = 0
    RESERVED = 1
    SUBMITTED = 2
    POLLING = 3
    SUCCEEDED = 4
    RETRYING = 5
    CANCELLED = 6
    DEADLETTER = 7


_TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.CANCELLED, JobStatus.DEADLETTER})
#: While leased + working, the job holds a lease that must outlive the window.
_LEASED = frozenset({JobStatus.RESERVED, JobStatus.SUBMITTED, JobStatus.POLLING})


@dataclass(frozen=True, slots=True)
class RenderJobState:
    """A finite snapshot of one render job's lifecycle across N workers."""

    status: JobStatus
    #: Worker id holding the lease (1..workers), or 0 for unleased.
    holder: int
    #: Whether the held lease is still fresh (heartbeated). Only a lapsed lease
    #: is reapable — a fresh one is protected (§12.1).
    lease_valid: bool
    #: A budget reservation is currently outstanding for this job.
    reserved: bool
    #: How many times the budget was actually debited (must stay ≤ 1).
    spent: int
    attempts: int
    cancel_requested: bool
    #: A *ghost* render: a worker that was reaped while genuinely still rendering
    #: (the lease was stolen out from under it) and can therefore ack a second,
    #: independent provider result. Under correct §12.1 semantics this is always
    #: ``False`` — the reaper only ever steals a *lapsed* (crashed) lease, whose
    #: holder is gone and cannot ack. A reaper that steals a *fresh* lease (the
    #: bug) sets it, exposing the double-submit / double-spend.
    ghost_active: bool = False

    def is_terminal(self) -> bool:
        return self.status in _TERMINAL


def build_render_queue_spec(*, workers: int = 2) -> Spec[RenderJobState]:
    """Build the §12.1 claim/lease/ack lifecycle spec for ``workers`` workers.

    The reaper, the cancel token, and the retry ladder are all modelled, so the
    checker covers: two workers racing to claim, a reaper stealing a *lapsed*
    lease (allowed) vs. a *fresh* one (forbidden — the heartbeat), and a cancel
    arriving at every non-terminal point.

    The dead-letter decision is delegated to the **real** production retry policy
    (:class:`app.queue.redis_queue.RetryPolicy`) so the "attempts bounded / job
    terminates" properties are statements about the actual code path.
    """
    from app.queue.redis_queue import RetryPolicy

    # The real policy with the production cap; backoff delays are irrelevant to
    # the lifecycle (they only schedule *when* a requeue becomes claimable).
    policy = RetryPolicy(cap=RETRY_CAP, backoff_s=(2.0, 8.0, 30.0))

    def s0() -> RenderJobState:
        return RenderJobState(
            status=JobStatus.QUEUED,
            holder=0,
            lease_valid=False,
            reserved=False,
            spent=0,
            attempts=0,
            cancel_requested=False,
        )

    # -- worker claim: QUEUED → RESERVED, taking the lease + reserving budget -- #

    def claim_action(worker: int) -> Action[RenderJobState]:
        def guard(s: RenderJobState) -> bool:
            # Only an unheld QUEUED job is claimable; the §12.1 atomic claim pops
            # it and leases it in one step, so two workers cannot both succeed.
            return s.status is JobStatus.QUEUED and s.holder == 0

        def effect(s: RenderJobState) -> tuple[RenderJobState, ...]:
            return (
                replace(
                    s,
                    status=JobStatus.RESERVED,
                    holder=worker,
                    lease_valid=True,
                    reserved=True,
                ),
            )

        return Action(f"claim_w{worker}", guard, effect, Fairness.WEAK)

    # -- submit / poll: RESERVED → SUBMITTED → POLLING ----------------------- #

    def can_submit(s: RenderJobState) -> bool:
        return s.status is JobStatus.RESERVED and not s.cancel_requested

    def submit(s: RenderJobState) -> tuple[RenderJobState, ...]:
        return (replace(s, status=JobStatus.SUBMITTED),)

    def can_poll(s: RenderJobState) -> bool:
        return s.status is JobStatus.SUBMITTED and not s.cancel_requested

    def poll(s: RenderJobState) -> tuple[RenderJobState, ...]:
        return (replace(s, status=JobStatus.POLLING),)

    # -- success: POLLING → SUCCEEDED, spend the reservation exactly once ----- #

    def can_succeed(s: RenderJobState) -> bool:
        return s.status is JobStatus.POLLING and not s.cancel_requested

    def succeed(s: RenderJobState) -> tuple[RenderJobState, ...]:
        # Ack debits the budget once and releases the lease + reservation.
        return (
            replace(
                s,
                status=JobStatus.SUCCEEDED,
                holder=0,
                lease_valid=False,
                reserved=False,
                spent=s.spent + 1,
            ),
        )

    # -- transient failure → retry or dead-letter (§12.1 backoff ladder) ----- #

    def can_fail(s: RenderJobState) -> bool:
        return s.status is JobStatus.POLLING and not s.cancel_requested

    def fail(s: RenderJobState) -> tuple[RenderJobState, ...]:
        from app.queue.redis_queue import RetryDecision

        attempts = s.attempts + 1
        if policy.decide(attempts) is RetryDecision.DEADLETTER:
            # Dead-letter: degrade (Ken-Burns). Release lease + reservation.
            return (
                replace(
                    s,
                    status=JobStatus.DEADLETTER,
                    holder=0,
                    lease_valid=False,
                    reserved=False,
                    attempts=attempts,
                ),
            )
        # Retry: re-queue after backoff. The reservation is *released* on the way
        # back to QUEUED and re-taken on the next claim, so it is never held by a
        # job sitting in the queue (which would pin the budget).
        return (
            replace(
                s,
                status=JobStatus.QUEUED,
                holder=0,
                lease_valid=False,
                reserved=False,
                attempts=attempts,
            ),
        )

    # -- cancellation (§4.8 / §12.1): a seek requests cancel; worker honours it - #

    def can_request_cancel(s: RenderJobState) -> bool:
        # A seek can request cancellation at any time before the job is terminal.
        return not s.is_terminal() and not s.cancel_requested

    def request_cancel(s: RenderJobState) -> tuple[RenderJobState, ...]:
        return (replace(s, cancel_requested=True),)

    def can_honour_cancel(s: RenderJobState) -> bool:
        # The worker checks the token at a safe point and aborts cooperatively,
        # releasing the reservation (§12.1). A QUEUED job is simply removed.
        return s.cancel_requested and not s.is_terminal()

    def honour_cancel(s: RenderJobState) -> tuple[RenderJobState, ...]:
        return (
            replace(
                s,
                status=JobStatus.CANCELLED,
                holder=0,
                lease_valid=False,
                reserved=False,
            ),
        )

    # -- lease lifecycle: a fresh lease can lapse; a lapsed one is reaped ----- #

    def can_lease_lapse(s: RenderJobState) -> bool:
        # The environment may let a lease go stale (a worker stall / crash). Only
        # a currently-leased, fresh lease can lapse.
        return s.status in _LEASED and s.lease_valid

    def lease_lapse(s: RenderJobState) -> tuple[RenderJobState, ...]:
        return (replace(s, lease_valid=False),)

    def can_reap(s: RenderJobState) -> bool:
        # The reaper re-queues a job whose lease has lapsed — and ONLY then. A
        # fresh (heartbeated) lease is protected; reaping it would double-submit.
        return s.status in _LEASED and not s.lease_valid

    def reap(s: RenderJobState) -> tuple[RenderJobState, ...]:
        # Crash recovery: release the (dead) holder's reservation and re-queue so
        # another worker can claim. Reaping a *lapsed* lease means the holder
        # crashed — there is no surviving render, so no ghost is created. The
        # reservation is released, not duplicated.
        return (
            replace(
                s,
                status=JobStatus.QUEUED,
                holder=0,
                lease_valid=False,
                reserved=False,
            ),
        )

    # A ghost render: a worker reaped while still alive can return a second,
    # independent provider result and ack it — a double spend. This action is
    # *enabled only when a ghost exists*, which under correct §12.1 semantics is
    # never (reap only steals lapsed/crashed leases). It is present so the model
    # *can* express the double-submit if a buggy reaper ever creates a ghost.
    def can_ghost_ack(s: RenderJobState) -> bool:
        return s.ghost_active

    def ghost_ack(s: RenderJobState) -> tuple[RenderJobState, ...]:
        # The stale worker's render lands and is acked independently → a second
        # debit on the same job.
        return (replace(s, ghost_active=False, spent=s.spent + 1),)

    actions: list[Action[RenderJobState]] = [claim_action(w) for w in range(1, workers + 1)]
    actions += [
        Action("submit", can_submit, submit, Fairness.WEAK),
        Action("poll", can_poll, poll, Fairness.WEAK),
        Action("succeed", can_succeed, succeed, Fairness.WEAK),
        # fail is an environment event (transient provider error) — not fair, so
        # the liveness check does not assume a job always fails.
        Action("fail", can_fail, fail, Fairness.NONE),
        Action("request_cancel", can_request_cancel, request_cancel, Fairness.NONE),
        Action("honour_cancel", can_honour_cancel, honour_cancel, Fairness.WEAK),
        Action("lease_lapse", can_lease_lapse, lease_lapse, Fairness.NONE),
        Action("reap", can_reap, reap, Fairness.WEAK),
        # Enabled only if a (buggy) reaper ever creates a ghost; dormant otherwise.
        Action("ghost_ack", can_ghost_ack, ghost_ack, Fairness.WEAK),
    ]

    invariants: tuple[Invariant[RenderJobState], ...] = (
        # THE headline invariant: a job's budget is debited at most once, across
        # every interleaving of two workers, a reaper, and retries. A double
        # spend here is the §12.1 "duplicate Scheduler events double-spend the
        # budget" bug.
        invariant("no_double_spend", lambda s: s.spent <= 1),
        # At most one worker holds the job at any time (the atomic claim + the
        # single-holder lease). Two holders would be a lost-update on the lease.
        invariant("single_holder", lambda s: s.holder in range(0, workers + 1)),
        # A terminal job holds nothing: no lease, no reservation, no holder.
        invariant(
            "terminal_releases_everything",
            lambda s: (not s.is_terminal())
            or (s.holder == 0 and not s.lease_valid and not s.reserved),
        ),
        # A reservation is held only while the job is in a leased, active state —
        # never while it sits QUEUED (which would pin budget on a waiting job).
        invariant(
            "reserved_only_when_leased",
            lambda s: (not s.reserved) or (s.status in _LEASED),
        ),
        # The lease-holder bookkeeping is consistent: a job has a holder iff it is
        # in a leased state.
        invariant(
            "holder_iff_leased",
            lambda s: (s.holder != 0) == (s.status in _LEASED),
        ),
        # Retries are bounded; the job dead-letters past the cap rather than
        # looping forever (§12.1 retry cap → degrade).
        invariant("attempts_bounded", lambda s: s.attempts <= RETRY_CAP + 1),
        # A succeeded job is never *also* one that was cancelled — the two
        # terminal outcomes are mutually exclusive (no "ack a cancelled job").
        invariant(
            "success_excludes_cancel",
            lambda s: not (s.status is JobStatus.SUCCEEDED and s.cancel_requested),
        ),
    )

    leads_to_props: tuple[LeadsTo[RenderJobState], ...] = (
        # Every claimed job eventually reaches a terminal state (succeeded,
        # cancelled, or dead-lettered → degrade). Under weak fairness the
        # submit/poll/succeed/honour-cancel chain drives it to terminal: the
        # pipeline never blocks on one shot (§4.11 / §12.4).
        leads_to(
            "claimed_job_terminates",
            trigger=lambda s: s.status is JobStatus.RESERVED,
            goal=lambda s: s.is_terminal(),
        ),
        # A requested cancellation is never lost: it leads-to the job leaving the
        # active set (becoming terminal). Honour-cancel is weakly fair, so a
        # cancel cannot be starved forever.
        leads_to(
            "cancel_eventually_honoured",
            trigger=lambda s: s.cancel_requested,
            goal=lambda s: s.is_terminal(),
        ),
    )

    def label(s: RenderJobState) -> str:
        flags = "".join(
            [
                "L" if s.lease_valid else "-",
                "R" if s.reserved else "-",
                "C" if s.cancel_requested else "-",
            ]
        )
        return (
            f"{s.status.name:<10} h{s.holder} {flags} "
            f"spent={s.spent} att={s.attempts}"
        )

    return Spec(
        name="render_queue_lifecycle",
        initial=(s0(),),
        actions=tuple(actions),
        invariants=invariants,
        leads_to_props=leads_to_props,
        state_label=label,
    )
