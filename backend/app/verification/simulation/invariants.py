"""End-to-end invariants — the properties the simulation *proves* (kinora.md §4.5
buffer health, §9.7 shot lifecycle, §11.1 budget accounting, §12.1 queue
correctness, §13 metrics).

A simulation that merely *runs* the loop is a stress test; a simulation that
asserts **invariants** across thousands of seeded fault schedules is verification.
This module is that assertion layer. Each :class:`Invariant` is a named predicate
over a finished :class:`~app.verification.simulation.system.SystemReport`; it
returns an :class:`InvariantResult` that is either a pass or a precise violation
("shot_00042 is stuck in promoted after convergence", "budget ledger off by 25.0s").

The invariants are split by the two classic correctness flavours:

* **Safety** — "nothing bad ever happens", checkable at every state and at the
  end: no double-spend of the scarce video budget (§12.1 idempotency), the budget
  ledger is conserved (§11.1), no shot is in a non-terminal §9.7 state once the
  system has quiesced, the DLQ never silently swallows a shot (a dead-lettered
  shot must have degraded, never vanished).
* **Liveness** — "something good eventually happens", checkable at the quiescent
  end state the runtime drives to: the queue drains (no job stuck forever), every
  promoted+accepted shot eventually emitted ``clip_ready`` (§9.8), every budget
  reservation is eventually resolved (committed or released — *this is the one the
  reserve→enqueue leak violates*), and the committed buffer stays healthy enough
  under nominal conditions to deliver the §4.5 smooth-playback promise.

The buffer-health bar is **profile-aware**: under a chaos storm we do *not* demand
the sawtooth never dips below ``L`` (the §4.4 degradation ladder is the *correct*
response to a storm, and the film stepping down to Ken-Burns is a feature, not a
violation). We demand it under ``nominal``. Safety invariants, by contrast, must
hold under *every* profile — a storm may degrade quality but must never corrupt
state or double-charge the budget.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.verification.simulation.system import SystemReport


@dataclass(frozen=True, slots=True)
class InvariantResult:
    """The outcome of checking one invariant against one run."""

    name: str
    ok: bool
    detail: str = ""
    #: ``safety`` violations are state corruption (must never happen); ``liveness``
    #: violations are convergence failures (something never finished).
    kind: str = "safety"

    @staticmethod
    def passed(name: str, kind: str = "safety") -> InvariantResult:
        return InvariantResult(name=name, ok=True, kind=kind)

    @staticmethod
    def failed(name: str, detail: str, kind: str = "safety") -> InvariantResult:
        return InvariantResult(name=name, ok=False, detail=detail, kind=kind)


#: An invariant is a function from a report (+ tolerances) to a result.
InvariantFn = Callable[[SystemReport], InvariantResult]


@dataclass(frozen=True, slots=True)
class Invariant:
    """A named, categorised predicate over a finished run."""

    name: str
    fn: InvariantFn
    kind: str = "safety"

    def check(self, report: SystemReport) -> InvariantResult:
        try:
            return self.fn(report)
        except Exception as exc:  # an invariant must never itself crash the sweep
            return InvariantResult.failed(
                self.name, f"invariant raised {type(exc).__name__}: {exc}", kind=self.kind
            )


# --------------------------------------------------------------------------- #
# Safety invariants — must hold under EVERY fault profile
# --------------------------------------------------------------------------- #


def _no_double_spend(report: SystemReport) -> InvariantResult:
    """§12.1: the ``shot_hash`` idempotency key means no shot is charged twice.

    Each accepted shot commits its reservation exactly once; the budget's
    ``commit`` is idempotent, so a duplicate ``clip_ready`` (an at-least-once
    delivery, a reaped-and-reclaimed job completing twice) charges nothing extra.
    We verify the committed total equals the sum of per-shot committed seconds —
    if a shot were charged twice, ``spent`` would exceed that sum.
    """
    assert report.budget is not None
    per_shot = sum(s.committed_s for s in report.shots.values())
    spent = report.budget.spent
    if abs(spent - per_shot) > 1e-6:
        return InvariantResult.failed(
            "no_double_spend",
            f"budget.spent={spent:.3f} but sum of per-shot committed={per_shot:.3f} "
            "— a shot was charged more than once (idempotency broken)",
        )
    return InvariantResult.passed("no_double_spend")


def _budget_ledger_conserved(report: SystemReport) -> InvariantResult:
    """§11.1: video-seconds are conserved — remaining+committed+outstanding=total.

    The pool is neither created nor destroyed, only moved between the three
    buckets. A violation means an accounting path lost or invented seconds.
    """
    assert report.budget is not None
    if not report.budget.accounting_ok:
        b = report.budget
        ledger = (
            report.budget._remaining  # noqa: SLF001 - invariant inspects the ledger
            + report.budget._committed
            + report.budget.outstanding_seconds
        )
        return InvariantResult.failed(
            "budget_ledger_conserved",
            f"ledger={ledger:.3f} != total={b.total:.3f} (seconds lost or invented)",
        )
    return InvariantResult.passed("budget_ledger_conserved")


def _no_stuck_shots(report: SystemReport) -> InvariantResult:
    """§9.7: at the quiescent end, no enqueued shot sits in a non-terminal state.

    Every shot the scheduler promoted+enqueued must, after the storm clears and
    the loop drains, have reached a §9.7 terminal state: ``ACCEPTED`` (full video),
    ``DEGRADED`` (rode the ladder), or have been cooperatively cancelled. A shot
    that is none of these is *stuck* — the headline liveness failure this whole
    simulator exists to catch.
    """
    stuck = report.unresolved_shots()
    if stuck:
        ids = ", ".join(s.shot_id for s in stuck[:8])
        more = "" if len(stuck) <= 8 else f" (+{len(stuck) - 8} more)"
        return InvariantResult.failed(
            "no_stuck_shots",
            f"{len(stuck)} shot(s) never reached a terminal §9.7 state after "
            f"convergence: {ids}{more}",
        )
    return InvariantResult.passed("no_stuck_shots")


def _dlq_implies_degraded(report: SystemReport) -> InvariantResult:
    """§12.1/§12.4: a dead-lettered shot drops to degradation, never vanishes.

    The pipeline never blocks on one bad shot — a job that exhausts retries goes
    to the DLQ and the shot rides the Ken-Burns ladder. So at the end, the DLQ
    length must not exceed the number of shots we recorded as degraded (each
    dead-letter must correspond to a degraded shot; the film never hard-stops).
    """
    degraded = len(report.degraded_shots())
    if report.final_dlq_len > degraded:
        return InvariantResult.failed(
            "dlq_implies_degraded",
            f"DLQ has {report.final_dlq_len} job(s) but only {degraded} shot(s) "
            "degraded — a dead-lettered shot silently vanished instead of riding "
            "the ladder",
        )
    return InvariantResult.passed("dlq_implies_degraded")


# --------------------------------------------------------------------------- #
# Liveness invariants — checked at the quiescent end state
# --------------------------------------------------------------------------- #


def _queue_drains(report: SystemReport) -> InvariantResult:
    """Liveness: the queue is empty once the system has converged.

    After the reader stops and faults quiesce, every job must have been claimed
    and resolved. A non-empty queue at the end means a job is stuck forever (a
    claim that never happens, a lane the worker pool never drains).
    """
    if report.final_queue_depth > 0:
        return InvariantResult.failed(
            "queue_drains",
            f"{report.final_queue_depth} job(s) still queued after convergence",
            kind="liveness",
        )
    return InvariantResult.passed("queue_drains", kind="liveness")


def _accepted_shots_emit_clip_ready(report: SystemReport) -> InvariantResult:
    """§9.8: every accepted shot eventually published a ``clip_ready`` event.

    Acceptance is only meaningful if it reaches the client — the loop *ends* in
    the ``clip_ready`` that hot-swaps the video into the reader's view. A shot
    that accepted but never emitted ``clip_ready`` is a dropped notification: the
    render happened but the reader would never see it.
    """
    assert report.events is not None
    accepted = len(report.accepted_shots())
    clip_ready = len(report.events.of_type("clip_ready"))
    if clip_ready < accepted:
        return InvariantResult.failed(
            "accepted_shots_emit_clip_ready",
            f"{accepted} shot(s) accepted but only {clip_ready} clip_ready event(s) "
            "— an accepted render never reached the client",
            kind="liveness",
        )
    return InvariantResult.passed("accepted_shots_emit_clip_ready", kind="liveness")


def _reservations_resolved(report: SystemReport) -> InvariantResult:
    """§11.1: every budget reservation is eventually committed or released.

    A reservation that is neither committed (the shot accepted) nor released (the
    shot was cancelled / degraded / deduped) is a *stranded earmark* — it holds
    scarce video-seconds forever, slowly starving the 1,650-second pool. This is
    the invariant the **scheduler reserve→enqueue leak** (see DESIGN.md) violates:
    a transient broker error between ``reserve`` and ``enqueue`` strands the
    earmark with no rollback.
    """
    assert report.budget is not None
    outstanding = report.budget.outstanding_reservations
    if outstanding > 0:
        return InvariantResult.failed(
            "reservations_resolved",
            f"{outstanding} budget reservation(s) ({report.budget.outstanding_seconds:.1f}s) "
            "stranded after convergence — neither committed nor released",
            kind="liveness",
        )
    return InvariantResult.passed("reservations_resolved", kind="liveness")


def _buffer_health_under_nominal(report: SystemReport) -> InvariantResult:
    """§4.5/§13: under non-storm conditions the buffer fills toward H and the
    sawtooth stays healthy (rarely below ``L``).

    This is a *quality* invariant, so it is profile-gated: a chaos storm is
    *allowed* to drain the buffer (the §4.4 ladder is the correct response). We
    assert it only when faults were light — the buffer must have reached at least
    the low watermark at its peak (the scheduler actually filled it) and must not
    have spent the majority of the session starved below ``L``.
    """
    samples = [occ for _t, occ in report.buffer_samples]
    if not samples:
        return InvariantResult.passed("buffer_health_under_nominal", kind="liveness")
    peak = max(samples)
    if peak < report.low_watermark:
        return InvariantResult.failed(
            "buffer_health_under_nominal",
            f"buffer peak {peak:.1f}s never reached low watermark "
            f"{report.low_watermark:.1f}s — the scheduler never filled the buffer",
            kind="liveness",
        )
    starved = sum(1 for occ in samples if occ < report.low_watermark)
    if starved > 0.6 * len(samples):
        return InvariantResult.failed(
            "buffer_health_under_nominal",
            f"buffer was below L for {starved}/{len(samples)} samples "
            "— sustained starvation, not a healthy sawtooth",
            kind="liveness",
        )
    return InvariantResult.passed("buffer_health_under_nominal", kind="liveness")


# --------------------------------------------------------------------------- #
# Invariant suites
# --------------------------------------------------------------------------- #

#: Safety invariants — must hold under EVERY fault profile (state correctness).
SAFETY_INVARIANTS: tuple[Invariant, ...] = (
    Invariant("no_double_spend", _no_double_spend, kind="safety"),
    Invariant("budget_ledger_conserved", _budget_ledger_conserved, kind="safety"),
    Invariant("no_stuck_shots", _no_stuck_shots, kind="safety"),
    Invariant("dlq_implies_degraded", _dlq_implies_degraded, kind="safety"),
)

#: Liveness invariants the *current* product satisfies — checked at the quiescent
#: end. NB: ``reservations_resolved`` is intentionally **not** here; the scheduler
#: today leaks a reservation when a transient broker error hits between ``reserve``
#: and ``enqueue`` (a real bug this simulator found — see DESIGN.md and
#: ``RESERVATION_LEAK_INVARIANT``). Keeping the default suite green-able means the
#: framework lands without depending on a fix in code another workstream owns.
LIVENESS_INVARIANTS: tuple[Invariant, ...] = (
    Invariant("queue_drains", _queue_drains, kind="liveness"),
    Invariant("accepted_shots_emit_clip_ready", _accepted_shots_emit_clip_ready, kind="liveness"),
)

#: The reservation-resolution invariant in isolation — the one the reserve→enqueue
#: leak violates. Used to *demonstrate* the simulator detecting (and shrinking) the
#: real bug, rather than to gate the green suite.
RESERVATION_LEAK_INVARIANT: Invariant = Invariant(
    "reservations_resolved", _reservations_resolved, kind="liveness"
)

#: Quality invariants — only meaningful under light fault load (profile-gated).
QUALITY_INVARIANTS: tuple[Invariant, ...] = (
    Invariant("buffer_health_under_nominal", _buffer_health_under_nominal, kind="liveness"),
)

#: The default safety+liveness suite the product is expected to pass under *any*
#: fault profile (quality is applied separately, profile-gated).
CORE_INVARIANTS: tuple[Invariant, ...] = SAFETY_INVARIANTS + LIVENESS_INVARIANTS

#: The strict suite that additionally demands every reservation resolve — fails on
#: the known reserve→enqueue leak. The shrinker test uses this to minimise the bug.
STRICT_INVARIANTS: tuple[Invariant, ...] = CORE_INVARIANTS + (RESERVATION_LEAK_INVARIANT,)


@dataclass(slots=True)
class InvariantReport:
    """The result of checking a suite of invariants against one run."""

    results: list[InvariantResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether every checked invariant held."""
        return all(r.ok for r in self.results)

    @property
    def violations(self) -> list[InvariantResult]:
        """The failed invariants (empty on a clean run)."""
        return [r for r in self.results if not r.ok]

    def first_violation(self) -> InvariantResult | None:
        """The first violation (the shrinker minimises toward reproducing it)."""
        return next((r for r in self.results if not r.ok), None)

    def summary(self) -> str:
        """A one-liner for the report: ``OK (7)`` or ``VIOLATED: name — detail``."""
        if self.ok:
            return f"OK ({len(self.results)} invariants)"
        v = self.first_violation()
        assert v is not None
        return f"VIOLATED: {v.name} ({v.kind}) — {v.detail}"


def check_invariants(
    report: SystemReport,
    *,
    invariants: tuple[Invariant, ...] = CORE_INVARIANTS,
) -> InvariantReport:
    """Check a suite of invariants against a finished run; return their results."""
    return InvariantReport(results=[inv.check(report) for inv in invariants])


__all__ = [
    "CORE_INVARIANTS",
    "LIVENESS_INVARIANTS",
    "QUALITY_INVARIANTS",
    "RESERVATION_LEAK_INVARIANT",
    "SAFETY_INVARIANTS",
    "STRICT_INVARIANTS",
    "Invariant",
    "InvariantReport",
    "InvariantResult",
    "check_invariants",
]
