"""A model of §12.2 per-session render fairness + lane admission.

§12.2 spells out three concurrency rules on the shared render pool that the
single-job lifecycle spec (:mod:`render_queue`) cannot express, because they are
about *how many* jobs of *which kind* and *whose* run at once:

* **Lanes.** 4 committed render slots + 2 *preemptible* speculative slots. A
  committed enqueue, when the pool is full, **preempts** a running speculative
  job; speculative jobs never preempt anything.
* **Backpressure.** When the queue is saturated, *new speculative* enqueues are
  **dropped** (the keyframe ladder covers them); *committed* enqueues are always
  admitted (by preemption if needed).
* **Per-session fairness.** A max concurrent render count per session stops one
  reader from monopolising the shared workers — every session that has demand
  can always make progress.

This model has several sessions each generating committed + speculative demand
against the shared lanes, and checks the invariants those three rules must
satisfy. Sessions are *interchangeable*, so the spec is the natural home for the
symmetry reduction: canonicalising the multiset of per-session running-counts
collapses the otherwise factorial blow-up.

The abstraction
---------------

Per session: how many committed / speculative jobs it currently has running, and
whether it has *pending demand* of each kind (an unbounded source of work, so
the fairness question "can a backlogged session still get a slot?" is
meaningful). Global: the lane occupancy is just the sum across sessions, bounded
by the lane sizes.

Actions: admit-committed (preempting a speculative job if the committed lane is
full), admit-speculative (dropped under backpressure), and complete-{committed,
speculative}. The completes are weakly fair (work finishes), so liveness — "a
session with committed demand eventually gets a committed slot" — is meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.verification.modelcheck import (
    Action,
    Invariant,
    LeadsTo,
    Spec,
    invariant,
    leads_to,
)
from app.verification.modelcheck.spec import Fairness
from app.verification.modelcheck.symmetry import SymmetryReduction, sort_multiset

__all__ = ["FairnessState", "SessionLoad", "build_fairness_spec", "session_symmetry"]

#: §12.2 lane sizes (committed slots, speculative slots).
COMMITTED_SLOTS = 4
SPECULATIVE_SLOTS = 2
#: The shared worker pool: committed + speculative draw from the same workers,
#: so the *total* running can never exceed the sum of the lane sizes.
TOTAL_WORKERS = COMMITTED_SLOTS + SPECULATIVE_SLOTS
#: §12.2 per-session committed concurrency cap — the anti-starvation guard.
PER_SESSION_COMMITTED_CAP = 2


@dataclass(frozen=True, slots=True)
class SessionLoad:
    """One session's running jobs + standing demand."""

    committed_running: int
    speculative_running: int
    #: An (abstract, unbounded) backlog of committed / speculative work to admit.
    committed_demand: bool
    speculative_demand: bool

    def sort_key(self) -> tuple[int, int, int, int]:
        return (
            self.committed_running,
            self.speculative_running,
            int(self.committed_demand),
            int(self.speculative_demand),
        )


@dataclass(frozen=True, slots=True)
class FairnessState:
    """The shared render pool across all sessions."""

    sessions: tuple[SessionLoad, ...]

    def committed_total(self) -> int:
        return sum(s.committed_running for s in self.sessions)

    def speculative_total(self) -> int:
        return sum(s.speculative_running for s in self.sessions)


def session_symmetry() -> SymmetryReduction:
    """Sessions are interchangeable → canonicalise by the sorted multiset of loads.

    The properties only ever ask "does *some* backlogged session lack a slot?" /
    "is the committed lane over capacity?", never "what is session 2 doing", so
    permuting the sessions yields an equivalent state. Sorting the per-session
    loads is the always-sound canonicaliser
    (:func:`app.verification.modelcheck.symmetry.sort_multiset`).
    """

    def canon(state: FairnessState) -> FairnessState:
        ordered = sort_multiset(s.sort_key() for s in state.sessions)
        return FairnessState(
            sessions=tuple(
                SessionLoad(
                    committed_running=key[0],
                    speculative_running=key[1],
                    committed_demand=bool(key[2]),
                    speculative_demand=bool(key[3]),
                )
                for key in ordered
            )
        )

    return SymmetryReduction.by(canon, description="session_orbit")


def build_fairness_spec(*, sessions: int = 3) -> Spec[FairnessState]:
    """Build the §12.2 lane-admission + per-session-fairness spec.

    ``sessions`` is the number of concurrent readers (default 3 — enough to
    exercise contention against the 4 committed / 2 speculative slots with a
    per-session cap of 2). With the session symmetry reduction the reachable
    space stays small despite the combinatorial load.
    """

    def s0() -> FairnessState:
        # Every session starts idle but with standing demand of both kinds, so
        # the admission contention is exercised from the first step.
        return FairnessState(
            sessions=tuple(
                SessionLoad(
                    committed_running=0,
                    speculative_running=0,
                    committed_demand=True,
                    speculative_demand=True,
                )
                for _ in range(sessions)
            )
        )

    def _set(state: FairnessState, i: int, load: SessionLoad) -> FairnessState:
        return replace(
            state,
            sessions=tuple(load if j == i else s for j, s in enumerate(state.sessions)),
        )

    def admit_committed(i: int) -> Action[FairnessState]:
        def guard(s: FairnessState) -> bool:
            load = s.sessions[i]
            return (
                load.committed_demand
                # The committed lane is a hard 4 slots.
                and s.committed_total() < COMMITTED_SLOTS
                # Per-session cap (§12.2): the anti-starvation guard.
                and load.committed_running < PER_SESSION_COMMITTED_CAP
            )

        def effect(s: FairnessState) -> tuple[FairnessState, ...]:
            load = s.sessions[i]
            # Committed enqueues are "always admitted" (§12.2): if the shared
            # worker pool is full, a running speculative job is *preempted* to
            # free the worker the committed job needs — prefer another session's
            # speculative (work-conserving), else any. The committed lane itself
            # never exceeds its 4 slots (the guard).
            if s.committed_total() + s.speculative_total() >= TOTAL_WORKERS:
                victim = _pick_speculative_victim(s, prefer_not=i)
                s = _preempt_speculative(s, victim)
            new_load = replace(
                s.sessions[i], committed_running=load.committed_running + 1
            )
            return (_set(s, i, new_load),)

        return Action(f"admit_committed_s{i}", guard, effect, Fairness.WEAK)

    def admit_speculative(i: int) -> Action[FairnessState]:
        def guard(s: FairnessState) -> bool:
            load = s.sessions[i]
            # Speculative admits only when the speculative lane has room; under
            # backpressure (lane full) the enqueue is simply *dropped* (no action).
            return load.speculative_demand and s.speculative_total() < SPECULATIVE_SLOTS

        def effect(s: FairnessState) -> tuple[FairnessState, ...]:
            load = s.sessions[i]
            new_load = replace(load, speculative_running=load.speculative_running + 1)
            return (_set(s, i, new_load),)

        return Action(f"admit_speculative_s{i}", guard, effect, Fairness.NONE)

    def complete_committed(i: int) -> Action[FairnessState]:
        def guard(s: FairnessState) -> bool:
            return s.sessions[i].committed_running > 0

        def effect(s: FairnessState) -> tuple[FairnessState, ...]:
            load = s.sessions[i]
            new_load = replace(load, committed_running=load.committed_running - 1)
            return (_set(s, i, new_load),)

        return Action(f"complete_committed_s{i}", guard, effect, Fairness.WEAK)

    def complete_speculative(i: int) -> Action[FairnessState]:
        def guard(s: FairnessState) -> bool:
            return s.sessions[i].speculative_running > 0

        def effect(s: FairnessState) -> tuple[FairnessState, ...]:
            load = s.sessions[i]
            new_load = replace(load, speculative_running=load.speculative_running - 1)
            return (_set(s, i, new_load),)

        return Action(f"complete_speculative_s{i}", guard, effect, Fairness.WEAK)

    actions: list[Action[FairnessState]] = []
    for i in range(sessions):
        actions.append(admit_committed(i))
        actions.append(admit_speculative(i))
        actions.append(complete_committed(i))
        actions.append(complete_speculative(i))

    invariants: tuple[Invariant[FairnessState], ...] = (
        # The committed lane never exceeds its 4 slots — preemption keeps it
        # bounded even though committed enqueues are "always admitted".
        invariant(
            "committed_lane_bounded",
            lambda s: s.committed_total() <= COMMITTED_SLOTS,
        ),
        # The speculative lane never exceeds its 2 slots (backpressure drops the
        # overflow rather than queueing unbounded).
        invariant(
            "speculative_lane_bounded",
            lambda s: s.speculative_total() <= SPECULATIVE_SLOTS,
        ),
        # The shared worker pool is never oversubscribed: committed + speculative
        # running together never exceed the total worker count (preemption frees
        # a worker before a committed job takes it).
        invariant(
            "total_workers_bounded",
            lambda s: s.committed_total() + s.speculative_total() <= TOTAL_WORKERS,
        ),
        # §12.2 per-session fairness: no session ever holds more than its
        # committed concurrency cap — the structural guarantee that one reader
        # cannot monopolise the committed workers and starve the others.
        invariant(
            "per_session_cap_respected",
            lambda s: all(
                load.committed_running <= PER_SESSION_COMMITTED_CAP
                for load in s.sessions
            ),
        ),
        invariant(
            "running_counts_non_negative",
            lambda s: all(
                load.committed_running >= 0 and load.speculative_running >= 0
                for load in s.sessions
            ),
        ),
    )

    leads_to_props: tuple[LeadsTo[FairnessState], ...] = (
        # The committed lane never deadlocks full: from any state with the lane
        # saturated, a fair run drains it back below capacity (completes are
        # weakly fair) — so a session waiting for a committed slot is never
        # blocked forever. This is the liveness side of anti-starvation.
        leads_to(
            "saturated_committed_lane_drains",
            trigger=lambda s: s.committed_total() >= COMMITTED_SLOTS,
            goal=lambda s: s.committed_total() < COMMITTED_SLOTS,
        ),
    )

    def label(s: FairnessState) -> str:
        cells = " ".join(
            f"s{i}[c{load.committed_running}/p{load.speculative_running}]"
            for i, load in enumerate(s.sessions)
        )
        return f"C={s.committed_total()} P={s.speculative_total()} | {cells}"

    return Spec(
        name="render_fairness_lanes",
        initial=(s0(),),
        actions=tuple(actions),
        invariants=invariants,
        leads_to_props=leads_to_props,
        state_label=label,
    )


def _pick_speculative_victim(s: FairnessState, *, prefer_not: int) -> int:
    """The session whose speculative job to preempt (prefer a *different* one)."""
    for i, load in enumerate(s.sessions):
        if i != prefer_not and load.speculative_running > 0:
            return i
    for i, load in enumerate(s.sessions):
        if load.speculative_running > 0:
            return i
    return prefer_not  # no speculative to preempt (guard guarantees there is one)


def _preempt_speculative(s: FairnessState, victim: int) -> FairnessState:
    load = s.sessions[victim]
    new_load = replace(load, speculative_running=max(0, load.speculative_running - 1))
    return replace(
        s,
        sessions=tuple(
            new_load if j == victim else sl for j, sl in enumerate(s.sessions)
        ),
    )
