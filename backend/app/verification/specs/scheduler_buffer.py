"""A model of the Scheduler's dual-watermark promotion protocol (§4.5–§4.9).

The Scheduler is the control plane that decides *what to render right now* by
keeping a buffer of committed video-seconds between a low and a high watermark.
Its concurrency hazards are exactly the ones that don't show up in a single-step
unit test: a clip lands *while* the reader advances *while* a seek cancels the
trajectory *while* the idle timer fires. This model lets the checker walk every
interleaving of those events and prove the buffer invariants hold throughout.

The abstraction
---------------

Real seconds and word offsets are continuous; we discretise to make the state
space finite while preserving the protocol's structure:

* ``buffer`` — committed video-seconds *ahead of the playhead*, in integer
  "slots" (each slot ≈ one shot ≈ ``SLOT_S`` seconds). The watermark band is
  ``[L, H]`` slots; we use ``L=1, H=3`` (the real 25s/75s with a 25s shot).
* ``inflight`` — committed shots enqueued but not yet landed (0..H).
* ``reserved`` — video-seconds reserved from the budget for in-flight committed
  jobs. Each committed enqueue reserves one slot; landing it *converts* the
  reservation into a recorded *spend* (one debit), and a cancel *releases* it
  (no debit). This convert-vs-release distinction is exactly the §12.1
  no-double-spend contract.
* ``spent`` — cumulative video-seconds actually debited (landed clips). The
  budget cap is fixed; the live commitment is ``reserved + spent``.
* ``budget`` — the *fixed* video-second cap (the §11 free-tier ceiling, in
  slots). A committed enqueue is only admitted when ``reserved + spent < budget``
  (the §4.9 ``budget_ok`` gate). The headline invariant
  ``reserved + spent <= budget`` then proves the budget is never over-committed:
  a land that frees a reservation without recording its spend (the double-spend
  bug) would let later promotes re-reserve the same capacity and break it.
* ``bursting`` — the hysteresis flag (§4.5). A burst starts when ``buffer`` drops
  below ``L`` and runs until ``buffer + inflight`` reaches ``H``, then stops.
* ``idle`` — the §4.7 idle-pause flag. While idle, no new speculation is
  enqueued; in-flight committed jobs are allowed to finish.
* ``trajectory`` — a small integer token (§4.8). A seek bumps it; a clip that
  lands for a stale trajectory is dropped (its reservation released), which is
  how cancellation is modelled without losing the budget.

The actions are the §4.9 control-loop transitions plus the environment events
(reader advances, clip lands, seek, idle-pause, wake). The autonomous ones
(``land``, ``refill_burst``) are weakly fair so the liveness checks mean
something: a drained buffer is *eventually* refilled, not merely *can be*.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.verification.modelcheck import (
    Action,
    Invariant,
    LeadsTo,
    Liveness,
    Spec,
    eventually,
    invariant,
    leads_to,
)
from app.verification.modelcheck.spec import Fairness

__all__ = ["SchedulerState", "build_scheduler_buffer_spec"]

# Watermark band, in shot-slots (the real 25s/75s with ~25s shots → L=1, H=3).
LOW = 1
HIGH = 3
# Per-session committed render slots (§4.9: 4 committed slots; bounded here for
# a tractable state space while still exercising concurrent in-flight jobs).
MAX_INFLIGHT = 2


@dataclass(frozen=True, slots=True)
class SchedulerState:
    """A finite snapshot of one session's Scheduler control state."""

    #: Committed video-seconds ready *ahead of the playhead*, in slots.
    buffer: int
    #: Committed jobs enqueued but not yet landed.
    inflight: int
    #: Video-seconds reserved from the budget for the in-flight committed jobs.
    reserved: int
    #: Cumulative video-seconds actually debited (landed clips).
    spent: int
    #: The fixed video-second budget cap (slots).
    budget: int
    #: The §4.5 hysteresis flag — a burst is in progress.
    bursting: bool
    #: The §4.7 idle-pause flag.
    idle: bool
    #: The §4.8 trajectory token; a seek bumps it, stale landings are dropped.
    trajectory: int

    def total_ahead(self) -> int:
        """Buffer that will exist once everything in flight lands (§4.5)."""
        return self.buffer + self.inflight


def build_scheduler_buffer_spec(
    *,
    initial_budget: int = 4,
    max_advances: int = HIGH + 1,
    max_trajectory: int = 2,
) -> Spec[SchedulerState]:
    """Build the §4.5–§4.9 dual-watermark promotion spec.

    ``initial_budget`` bounds the budget (slots); ``max_trajectory`` bounds how
    many seeks may occur (each bumps the token). The defaults keep the reachable
    space in the low thousands while still covering: a burst from empty, a clip
    landing concurrently with an advance, a seek that strands in-flight jobs, and
    the idle-pause halting speculation.
    """

    def s0() -> SchedulerState:
        return SchedulerState(
            buffer=0,
            inflight=0,
            reserved=0,
            spent=0,
            budget=initial_budget,
            bursting=False,
            idle=False,
            trajectory=0,
        )

    # -- §4.9 control-loop actions ------------------------------------------ #

    def can_start_burst(s: SchedulerState) -> bool:
        # Wake + low buffer triggers a burst (§4.5). Not while idle.
        return (
            not s.idle
            and not s.bursting
            and s.total_ahead() < LOW
        )

    def start_burst(s: SchedulerState) -> tuple[SchedulerState, ...]:
        return (replace(s, bursting=True),)

    def can_promote(s: SchedulerState) -> bool:
        # While bursting, enqueue committed shots until total-ahead hits HIGH,
        # respecting the per-session in-flight cap and the budget gate (§4.9).
        return (
            s.bursting
            and not s.idle
            and s.total_ahead() < HIGH
            and s.inflight < MAX_INFLIGHT
            and s.reserved + s.spent < s.budget  # the no-over-commit gate (§4.9)
        )

    def promote(s: SchedulerState) -> tuple[SchedulerState, ...]:
        # Enqueue one committed shot: reserve one slot against the budget cap.
        return (replace(s, inflight=s.inflight + 1, reserved=s.reserved + 1),)

    def can_stop_burst(s: SchedulerState) -> bool:
        # Hit the high watermark → idle the committed lane (§4.5).
        return s.bursting and s.total_ahead() >= HIGH

    def stop_burst(s: SchedulerState) -> tuple[SchedulerState, ...]:
        return (replace(s, bursting=False),)

    # -- environment events -------------------------------------------------- #

    def can_land(s: SchedulerState) -> bool:
        return s.inflight > 0

    def land(s: SchedulerState) -> tuple[SchedulerState, ...]:
        # A committed clip lands: it stops being in-flight, its reservation
        # *converts* into a recorded spend (one debit), and the ready video-second
        # joins the buffer. reserved+spent is invariant under a land (one moves to
        # the other), which is the no-double-spend contract: the slot is counted
        # exactly once across its whole life.
        landed = replace(
            s,
            inflight=s.inflight - 1,
            reserved=s.reserved - 1,
            spent=s.spent + 1,
            buffer=min(s.buffer + 1, HIGH),
        )
        return (landed,)

    def can_advance(s: SchedulerState) -> bool:
        # Reader consumes one slot of buffer as the playhead moves forward.
        return s.buffer > 0

    def advance(s: SchedulerState) -> tuple[SchedulerState, ...]:
        return (replace(s, buffer=s.buffer - 1),)

    def can_idle_pause(s: SchedulerState) -> bool:
        return not s.idle

    def idle_pause(s: SchedulerState) -> tuple[SchedulerState, ...]:
        # §4.7: speculation halts; a burst in progress is stopped (committed
        # in-flight jobs are allowed to finish, modelled by leaving inflight).
        return (replace(s, idle=True, bursting=False),)

    def can_wake(s: SchedulerState) -> bool:
        return s.idle

    def wake(s: SchedulerState) -> tuple[SchedulerState, ...]:
        return (replace(s, idle=False),)

    def can_seek(s: SchedulerState) -> bool:
        # §4.8: a far seek bumps the trajectory token and re-seeds. In-flight
        # committed jobs for the *old* trajectory will be dropped when they land
        # (cancellation), releasing their reservations — never lost, never
        # double-spent. The buffer built for the old position is discarded.
        return s.trajectory < max_trajectory

    def seek(s: SchedulerState) -> tuple[SchedulerState, ...]:
        # Drop the stale buffer + reservations for in-flight jobs (cooperative
        # cancel releases budget — §12.1). Re-seed: not bursting, not idle.
        return (
            replace(
                s,
                trajectory=s.trajectory + 1,
                buffer=0,
                inflight=0,
                reserved=0,  # in-flight reservations released (cooperative cancel)
                # spent is NOT rolled back: already-landed clips were genuinely
                # paid for; a seek discards their *buffer* but not their debit.
                bursting=False,
                idle=False,
            ),
        )

    actions: tuple[Action[SchedulerState], ...] = (
        # Autonomous progress is weakly fair so liveness is meaningful.
        Action("start_burst", can_start_burst, start_burst, Fairness.WEAK),
        Action("promote", can_promote, promote, Fairness.WEAK),
        Action("stop_burst", can_stop_burst, stop_burst, Fairness.WEAK),
        Action("land", can_land, land, Fairness.WEAK),
        # Environment events are not fair (the reader need not advance/seek/idle).
        Action("advance", can_advance, advance, Fairness.NONE),
        Action("idle_pause", can_idle_pause, idle_pause, Fairness.NONE),
        Action("wake", can_wake, wake, Fairness.NONE),
        Action("seek", can_seek, seek, Fairness.NONE),
    )

    invariants: tuple[Invariant[SchedulerState], ...] = (
        # The buffer is video-seconds ahead of the playhead; it can never read as
        # a debt. This is the headline §4.5 safety property.
        invariant("buffer_non_negative", lambda s: s.buffer >= 0),
        invariant("inflight_non_negative", lambda s: s.inflight >= 0),
        invariant("reserved_non_negative", lambda s: s.reserved >= 0),
        invariant("spent_non_negative", lambda s: s.spent >= 0),
        # No over-commit / no double-spend: live commitment (in-flight
        # reservations + already-spent) never exceeds the fixed budget cap. If
        # this fails, the §4.9 budget gate is broken — a land that frees a
        # reservation without recording the spend lets a later promote re-reserve
        # the same capacity and over-commit the free-tier video-seconds.
        invariant("no_over_commit", lambda s: s.reserved + s.spent <= s.budget),
        # The committed lane never exceeds the per-session in-flight cap (§4.9).
        invariant("inflight_capped", lambda s: s.inflight <= MAX_INFLIGHT),
        # Hysteresis upper bound: total-ahead never overshoots the high watermark
        # by more than one in-flight promotion (we stop promoting at HIGH).
        invariant("buffer_bounded", lambda s: s.total_ahead() <= HIGH + 0),
        # While idle, the burst flag must be clear — §4.7 halts speculation.
        invariant("idle_implies_not_bursting", lambda s: (not s.idle) or (not s.bursting)),
    )

    liveness: tuple[Liveness[SchedulerState], ...] = (
        # The committed lane always settles: from any state, a fair run reaches a
        # quiescent point where no committed work is pending and the lane is idle
        # (not bursting). This is the "burst-then-idle sawtooth" of §4.10 — the
        # system does not generate forever.
        eventually(
            "settles_to_idle_lane",
            lambda s: s.inflight == 0 and not s.bursting,
        ),
    )

    leads_to_props: tuple[LeadsTo[SchedulerState], ...] = (
        # Every started burst eventually stops (it cannot burst forever). Under
        # weak fairness the land/promote/stop chain drives total-ahead to HIGH (or
        # the budget runs out), at which point the burst stops.
        leads_to(
            "burst_eventually_stops",
            trigger=lambda s: s.bursting,
            goal=lambda s: not s.bursting,
        ),
        # Every reservation is eventually released (a landed clip or a cancel),
        # so the budget is never permanently tied up by an in-flight job.
        leads_to(
            "reservation_eventually_released",
            trigger=lambda s: s.reserved > 0,
            goal=lambda s: s.reserved == 0,
        ),
    )

    def label(s: SchedulerState) -> str:
        flags = "".join(
            [
                "B" if s.bursting else "-",
                "I" if s.idle else "-",
            ]
        )
        return (
            f"buf={s.buffer} infl={s.inflight} resv={s.reserved} "
            f"spent={s.spent} bud={s.budget} {flags} traj={s.trajectory}"
        )

    # Cap advances implicitly via buffer<=HIGH; max_advances documents intent.
    _ = max_advances

    return Spec(
        name="scheduler_watermark_buffer",
        initial=(s0(),),
        actions=actions,
        invariants=invariants,
        liveness=liveness,
        leads_to_props=leads_to_props,
        state_label=label,
    )
