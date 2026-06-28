"""Runbooks-as-code — executable incident playbooks (kinora.md §4.11/§12.1).

§4.11 is the answer sheet for "what happens when…": each failure has a guarding
mechanism. A *runbook* turns that table from prose into an executable,
inspectable plan — an ordered list of :class:`RunbookStep`, each with a
human-readable action, a machine ``check`` (a predicate over a live signals dict)
that decides whether the step is needed, and an ``automation`` flag marking
whether it can run unattended.

Runbooks are *dry-run-first*: :meth:`Runbook.plan` evaluates the steps against a
signals snapshot and returns the ordered remediation it *would* take (which
checks tripped, which steps to run), without performing side effects — so an
operator (or a test) sees the plan before anything happens, and the registry of
standard Kinora incidents is itself unit-tested for correctness.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

#: A check over a live signals snapshot: returns True when this step is needed.
Check = Callable[[Mapping[str, object]], bool]


class Severity(StrEnum):
    """How urgent an incident is (drives paging vs. ticketing)."""

    PAGE = "page"  # wake someone up
    TICKET = "ticket"  # file for business hours
    INFO = "info"  # log / dashboard only


@dataclass(frozen=True, slots=True)
class RunbookStep:
    """One remediation step in a runbook.

    ``check`` decides whether the step applies to the current signals; a step with
    no check always applies. ``automation`` marks a step safe to run unattended
    (e.g. "drain the DLQ to Ken-Burns" is automatable; "rotate the DashScope key"
    is not). ``reference`` cites the kinora.md section the mechanism lives in.
    """

    title: str
    action: str
    check: Check | None = None
    automation: bool = False
    reference: str | None = None

    def applies(self, signals: Mapping[str, object]) -> bool:
        """Whether this step is needed given ``signals`` (no check => always)."""
        return True if self.check is None else bool(self.check(signals))


@dataclass(frozen=True, slots=True)
class PlannedStep:
    """A step the plan would execute, with why it tripped."""

    title: str
    action: str
    automation: bool
    reference: str | None

    def to_dict(self) -> dict[str, object]:
        """JSON projection."""
        return {
            "title": self.title,
            "action": self.action,
            "automation": self.automation,
            "reference": self.reference,
        }


@dataclass(frozen=True, slots=True)
class RunbookPlan:
    """The ordered remediation a runbook would take for a signals snapshot."""

    runbook: str
    severity: Severity
    triggered: bool
    steps: tuple[PlannedStep, ...]

    @property
    def automatable(self) -> tuple[PlannedStep, ...]:
        """The subset of planned steps safe to run unattended."""
        return tuple(s for s in self.steps if s.automation)

    @property
    def manual(self) -> tuple[PlannedStep, ...]:
        """The subset of planned steps that need a human."""
        return tuple(s for s in self.steps if not s.automation)

    def to_dict(self) -> dict[str, object]:
        """JSON projection of the plan."""
        return {
            "runbook": self.runbook,
            "severity": self.severity.value,
            "triggered": self.triggered,
            "steps": [s.to_dict() for s in self.steps],
        }

    def render_text(self) -> str:
        """A human-readable remediation plan."""
        head = (
            f"Runbook '{self.runbook}' [{self.severity.value}] — "
            f"{'TRIGGERED' if self.triggered else 'no action'}"
        )
        lines = [head]
        for i, step in enumerate(self.steps, 1):
            tag = "auto" if step.automation else "manual"
            ref = f" ({step.reference})" if step.reference else ""
            lines.append(f"  {i}. [{tag}] {step.title}{ref}")
            lines.append(f"      → {step.action}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Runbook:
    """A named incident playbook: a trigger + ordered remediation steps."""

    name: str
    severity: Severity
    summary: str
    #: When this predicate is true for the signals, the runbook is triggered.
    trigger: Check
    steps: Sequence[RunbookStep] = field(default_factory=tuple)

    def plan(self, signals: Mapping[str, object]) -> RunbookPlan:
        """Dry-run: the remediation this runbook would take for ``signals``.

        The plan is empty when the trigger is not met; otherwise it includes the
        ordered subset of steps whose checks apply. No side effects — this is the
        "look before you leap" surface an operator reviews.
        """
        triggered = bool(self.trigger(signals))
        planned: tuple[PlannedStep, ...] = ()
        if triggered:
            planned = tuple(
                PlannedStep(
                    title=s.title,
                    action=s.action,
                    automation=s.automation,
                    reference=s.reference,
                )
                for s in self.steps
                if s.applies(signals)
            )
        return RunbookPlan(
            runbook=self.name,
            severity=self.severity,
            triggered=triggered,
            steps=planned,
        )


# --------------------------------------------------------------------------- #
# Signal helpers (typed reads over the loose signals dict)
# --------------------------------------------------------------------------- #


def _num(signals: Mapping[str, object], key: str, default: float = 0.0) -> float:
    value = signals.get(key, default)
    return float(value) if isinstance(value, (int, float)) else default


def _flag(signals: Mapping[str, object], key: str, default: bool = False) -> bool:
    value = signals.get(key, default)
    return bool(value)


# --------------------------------------------------------------------------- #
# The standard Kinora incident registry (the §4.11 table, as code)
# --------------------------------------------------------------------------- #


def dlq_backlog_runbook(*, threshold: int = 5) -> Runbook:
    """Repeated render failures piling into the DLQ (§12.1/§4.11 'render fails')."""
    return Runbook(
        name="dlq_backlog",
        severity=Severity.TICKET,
        summary="Dead-letter queue is growing: shots are failing past the retry cap.",
        trigger=lambda s: _num(s, "dlq_len") >= threshold,
        steps=(
            RunbookStep(
                title="Confirm the pipeline is not blocked",
                action="Verify dead-lettered shots dropped to Ken-Burns (degradation "
                "ladder), so readers still see the film — the queue never blocks on "
                "one bad shot.",
                automation=True,
                reference="§12.4",
            ),
            RunbookStep(
                title="Check for a provider-wide failure",
                action="If DLQ growth correlates with provider_error_rate, treat as a "
                "DashScope incident, not a per-shot bug.",
                check=lambda s: _num(s, "provider_error_rate") > 0.1,
                reference="§12.1",
            ),
            RunbookStep(
                title="Drain + requeue transient dead-letters",
                action="Re-enqueue DLQ jobs whose error was transient once the provider "
                "recovers; leave deterministic failures degraded.",
                automation=True,
                reference="§12.1",
            ),
        ),
    )


def provider_rate_limit_runbook() -> Runbook:
    """DashScope 429 Throttling.RateQuota on the image model (§gotchas/§4.11)."""
    return Runbook(
        name="provider_rate_limit",
        severity=Severity.PAGE,
        summary="DashScope is returning 429 Throttling.RateQuota (image model).",
        trigger=lambda s: _num(s, "provider_error_rate") > 0.2
        or _flag(s, "provider_throttled"),
        steps=(
            RunbookStep(
                title="Confirm it is the image model, not video",
                action="The 429 is on the image-gen lane (keyframes/identity), independent "
                "of KINORA_LIVE_VIDEO; the video budget is unaffected.",
                reference="§gotchas",
            ),
            RunbookStep(
                title="Back off the keyframe lane",
                action="Lower the keyframe pool concurrency and rely on canon reference "
                "images / the Ken-Burns ladder while throttled.",
                automation=True,
                reference="§4.4/§12.4",
            ),
            RunbookStep(
                title="Verify committed renders still flow",
                action="Committed video promotion is gated on budget, not the image 429; "
                "confirm clip_ready events still land.",
                reference="§4.9",
            ),
        ),
    )


def buffer_stall_runbook(*, low_watermark_s: float = 25.0) -> Runbook:
    """The committed buffer drained below L — a visible stall risk (§4.5/§13)."""
    return Runbook(
        name="buffer_stall",
        severity=Severity.PAGE,
        summary="Committed buffer dropped below the low watermark; stall risk.",
        trigger=lambda s: _num(s, "committed_seconds_ahead", low_watermark_s)
        < low_watermark_s,
        steps=(
            RunbookStep(
                title="Confirm the keyframe ladder is covering the gap",
                action="A drained buffer should ride Ken-Burns over keyframes, not "
                "hard-stop; verify no visible stalls (buffer_health.stalls == 0).",
                reference="§4.4/§12.4",
            ),
            RunbookStep(
                title="Check render-worker capacity",
                action="If render utilisation is saturated, add committed slots / workers "
                "(see capacity.mmc_queue) so production exceeds consumption.",
                check=lambda s: _num(s, "render_utilisation") > 0.85,
                automation=False,
                reference="§4.9/§12.2",
            ),
            RunbookStep(
                title="Verify promotion is not suspended by a false skim",
                action="If trajectory_is_stable is flapping, the velocity estimate may be "
                "noisy; the buffer can't fill while promotion is suspended.",
                check=lambda s: _flag(s, "promotion_suspended"),
                reference="§4.6",
            ),
        ),
    )


def budget_low_runbook(*, floor_s: float = 120.0) -> Runbook:
    """Video budget approaching the floor (§11.1/§4.11 'budget runs low')."""
    return Runbook(
        name="budget_low",
        severity=Severity.TICKET,
        summary="Video-seconds budget is approaching the floor.",
        trigger=lambda s: _num(s, "budget_remaining_s", 1e9) < floor_s,
        steps=(
            RunbookStep(
                title="Confirm graceful degradation is engaged",
                action="Below the floor, generation rides the keyframe ladder and "
                "budget_low is surfaced to the UI; the film degrades, never stops.",
                automation=True,
                reference="§11.1/§12.4",
            ),
            RunbookStep(
                title="Lean on the shot cache",
                action="Re-reads and dedup (§8.7/§12.3) cost zero video-seconds; confirm "
                "cache hit ratio is healthy to stretch the runway.",
                reference="§8.7",
            ),
        ),
    )


def queue_backpressure_runbook(*, depth_threshold: int = 64) -> Runbook:
    """Render queue saturated — speculative enqueues being dropped (§12.2)."""
    return Runbook(
        name="queue_backpressure",
        severity=Severity.INFO,
        summary="Render queue depth high; speculative enqueues are being dropped.",
        trigger=lambda s: _num(s, "queue_depth") >= depth_threshold,
        steps=(
            RunbookStep(
                title="Confirm only speculative work is being shed",
                action="Backpressure drops *new speculative* enqueues (keyframe ladder "
                "covers them); committed enqueues are always admitted.",
                reference="§12.2",
            ),
            RunbookStep(
                title="Scale workers if committed wait is rising",
                action="If committed mean_wait is climbing, add workers; speculative "
                "shedding alone is by-design and not an incident.",
                check=lambda s: _num(s, "committed_mean_wait_s") > 5.0,
                automation=False,
                reference="§4.9/§12.2",
            ),
        ),
    )


def redis_partition_runbook() -> Runbook:
    """Redis (queue + scheduler control plane) unreachable (§12.1/§4.9)."""
    return Runbook(
        name="redis_partition",
        severity=Severity.PAGE,
        summary="Redis is unreachable: the queue + scheduler control plane is degraded.",
        trigger=lambda s: _flag(s, "redis_unreachable"),
        steps=(
            RunbookStep(
                title="Confirm durable mirrors are intact",
                action="Scheduler state mirrors to the sessions row and jobs mirror to "
                "render_jobs; on recovery, state rehydrates from Postgres.",
                reference="§4.9/§12.1",
            ),
            RunbookStep(
                title="Restore Redis connectivity",
                action="Failover / restart the Redis node; the scheduler runs in-process "
                "in the API and resumes once the control state reloads.",
                automation=False,
                reference="§process-model",
            ),
            RunbookStep(
                title="Reap orphaned leases after recovery",
                action="Run the queue reaper so jobs whose worker lease expired during the "
                "partition are re-queued (no double-submit).",
                automation=True,
                reference="§12.1",
            ),
        ),
    )


#: The standard Kinora runbook registry, keyed by incident name.
def standard_runbooks() -> dict[str, Runbook]:
    """Build the registry of standard Kinora incident runbooks (§4.11)."""
    books = [
        dlq_backlog_runbook(),
        provider_rate_limit_runbook(),
        buffer_stall_runbook(),
        budget_low_runbook(),
        queue_backpressure_runbook(),
        redis_partition_runbook(),
    ]
    return {b.name: b for b in books}


def triggered_runbooks(signals: Mapping[str, object]) -> list[RunbookPlan]:
    """Every standard runbook that triggers for ``signals``, as dry-run plans.

    The "what's on fire right now" view: feed a live signals snapshot and get the
    ordered set of remediation plans, most severe first (page > ticket > info).
    """
    severity_rank = {Severity.PAGE: 0, Severity.TICKET: 1, Severity.INFO: 2}
    plans = [rb.plan(signals) for rb in standard_runbooks().values()]
    fired = [p for p in plans if p.triggered]
    fired.sort(key=lambda p: severity_rank[p.severity])
    return fired


__all__ = [
    "Check",
    "PlannedStep",
    "Runbook",
    "RunbookPlan",
    "RunbookStep",
    "Severity",
    "budget_low_runbook",
    "buffer_stall_runbook",
    "dlq_backlog_runbook",
    "provider_rate_limit_runbook",
    "queue_backpressure_runbook",
    "redis_partition_runbook",
    "standard_runbooks",
    "triggered_runbooks",
]
