"""The crash-recovery + exactly-once render guard (kinora.md §9.7, §12.1).

This is the orchestration layer that *composes* the durability primitives —
:mod:`app.render.checkpoint` (resumable per-shot snapshots),
:mod:`app.render.durability.idempotency` (at-most-one live render per key),
:mod:`app.render.poison` (crash-loop quarantine), and the dead-letter sink — into
one wrapper around a per-shot render call.

A bare ``RenderPipeline.render_shot`` is correct on the happy path, but a worker
that crashes mid-render or a queue that re-delivers a job can re-run it. The guard
makes a single ``run`` call **safe to deliver more than once**:

1. **Idempotency** — admit at most one live render per ``(shot_id, spec_digest)``.
   A duplicate delivery for an in-flight key defers; for a completed key it serves
   the recorded result. Never double-renders, never double-spends.
2. **Resume** — probe the checkpoint store; a terminal checkpoint short-circuits to
   a no-op, a mid-flight one is handed to the render so it resumes from the last
   recorded state rather than restarting from the top.
3. **Crash isolation** — a hard render crash records a poison failure and releases
   the idempotency claim so a transient blip retries. Once the poison threshold is
   crossed the shot is **quarantined**: it is forced to the bottom (audio-text)
   rung so the film never hard-stops, the key is recorded completed (so it is never
   re-attempted into a crash-loop), and it is routed to the **dead-letter sink** for
   human triage.
4. **Checkpoint** — the guard writes a checkpoint as the render advances so a later
   crash resumes from there.

The render itself is injected as a callable so this module imports no pipeline,
provider, DB, or ffmpeg — the whole control flow is unit-testable with fakes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.observability import metrics
from app.render.checkpoint import (
    CheckpointStore,
    InMemoryCheckpointStore,
    ShotCheckpoint,
    probe_resume,
)
from app.render.durability.deadletter import DeadLetterSink, NullDeadLetterSink
from app.render.durability.idempotency import (
    Admission,
    IdempotencyGuard,
    IdempotencyKey,
    Lease,
)
from app.render.poison import PoisonTracker
from app.render.retry import FailureClass, classify_failure
from app.render.states import TERMINAL_STATES, RenderState
from app.render.telemetry import RenderEvent, TelemetryBus

logger = get_logger("app.render.durability.guard")

__all__ = [
    "DurableOutcome",
    "DurableRenderGuard",
    "GuardResult",
    "RenderCall",
    "ResumeContext",
]


class DurableOutcome(StrEnum):
    """How a guarded render resolved."""

    #: The render ran (or resumed) and produced a result.
    RENDERED = "rendered"
    #: A completed checkpoint or idempotency record short-circuited the render.
    SKIPPED = "skipped"
    #: A live worker holds the claim; this delivery deferred without rendering.
    DEFERRED = "deferred"
    #: The shot crossed the poison threshold and was dead-lettered.
    DEAD_LETTERED = "dead_lettered"


@dataclass(slots=True)
class ResumeContext:
    """What the guard hands the render call so it can resume mid-flight (§9.7).

    ``checkpoint`` is the last recorded snapshot (``None`` on a fresh render). The
    render call should resume from ``checkpoint.state`` using the step ledger inside
    it, and may call :meth:`checkpoint` to persist progress as it advances.
    """

    key: IdempotencyKey
    book_id: str
    shot_id: str
    checkpoint: ShotCheckpoint | None
    poisoned: bool
    _guard: DurableRenderGuard

    async def checkpoint_state(
        self, state: RenderState, *, attempt: int = 0, **fields: Any
    ) -> None:
        """Persist an intermediate checkpoint for this shot (called by the render)."""
        await self._guard.write_checkpoint(
            self.key, self.book_id, self.shot_id, state, attempt=attempt, **fields
        )


@dataclass(frozen=True, slots=True)
class GuardResult:
    """The result of :meth:`DurableRenderGuard.run`."""

    outcome: DurableOutcome
    #: The render call's own result (the pipeline's ``RenderResult``), if it ran.
    result: Any = None
    #: A recorded prior result served on a dedup/skip (JSON dict), if any.
    recorded: dict[str, Any] | None = None


#: The injected render. Receives the resume context (checkpoint + poison flag) and
#: returns a tuple of (the caller's result object, a small JSON-friendly summary to
#: record against the idempotency key for future dedup deliveries).
RenderCall = Callable[[ResumeContext], Awaitable[tuple[Any, dict[str, Any] | None]]]


class _ResultSummariser(Protocol):
    def __call__(self, result: Any) -> dict[str, Any] | None: ...


@dataclass(slots=True)
class DurableRenderGuard:
    """Wrap a per-shot render with checkpoint + idempotency + poison + dead-letter.

    Every collaborator is an injected, in-memory-testable seam. With the defaults
    (all in-memory) the guard is a self-contained, deterministic unit under test;
    production wires the Redis/DB-backed stores behind the same Protocols.

    Attributes:
        idempotency: the exactly-once admission gate (per ``(shot_id, spec)`` key).
        checkpoints: resumable per-shot snapshots (§9.7).
        poison: the crash-loop quarantine tracker.
        dead_letter: where a quarantined shot is routed for triage.
        bus: telemetry bus for checkpoint/resume/poison events (§12.5).
        settings: for the poison threshold + the checkpoint enable gate.
    """

    idempotency: IdempotencyGuard = field(default_factory=IdempotencyGuard)
    checkpoints: CheckpointStore = field(default_factory=InMemoryCheckpointStore)
    poison: PoisonTracker = field(default_factory=PoisonTracker)
    dead_letter: DeadLetterSink = field(default_factory=NullDeadLetterSink)
    bus: TelemetryBus | None = None
    settings: Settings | None = None

    def __post_init__(self) -> None:
        if self.settings is None:
            self.settings = get_settings()
        # Keep the poison threshold consistent with the configured value unless a
        # caller explicitly tuned the tracker (a non-default threshold).
        if self.poison.threshold == 3:  # the PoisonTracker dataclass default
            self.poison.threshold = self.settings.render_poison_threshold
        if self.bus is not None and self.poison.bus is None:
            self.poison.bus = self.bus

    @property
    def _checkpoint_enabled(self) -> bool:
        assert self.settings is not None
        return bool(self.settings.render_checkpoint_enabled)

    async def run(self, key: IdempotencyKey, book_id: str, render: RenderCall) -> GuardResult:
        """Run ``render`` for ``key`` at most once, resumably, crash-safely.

        The exactly-once + crash-recovery control flow described in the module
        docstring. ``render`` is invoked only when this delivery wins the
        idempotency claim and the shot is not already terminal/poisoned-out.
        """
        shot_id = key.shot_id

        # (1) Exactly-once admission. A duplicate delivery never re-renders.
        admission = self.idempotency.begin(key)
        if admission.admission is Admission.COMPLETED:
            return GuardResult(outcome=DurableOutcome.SKIPPED, recorded=admission.result)
        if admission.admission is Admission.IN_FLIGHT:
            return GuardResult(outcome=DurableOutcome.DEFERRED)
        assert admission.lease is not None
        lease = admission.lease

        # (2) Resume probe. A terminal checkpoint short-circuits to a no-op; the
        # claim is finalised completed so later deliveries also skip.
        decision = await probe_resume(self.checkpoints, shot_id)
        if decision.skip:
            self.idempotency.complete(lease, result=None)
            self._emit(RenderEvent.resumed(shot_id, RenderState.ACCEPTED, attempt=0))
            return GuardResult(outcome=DurableOutcome.SKIPPED)

        if decision.checkpoint is not None:
            self._emit(
                RenderEvent.resumed(
                    shot_id, decision.checkpoint.state, attempt=decision.checkpoint.attempts
                )
            )

        poisoned = self.poison.is_poisoned(shot_id)
        ctx = ResumeContext(
            key=key,
            book_id=book_id,
            shot_id=shot_id,
            checkpoint=decision.checkpoint,
            poisoned=poisoned,
            _guard=self,
        )

        # (3) Run + crash isolation.
        try:
            result, summary = await render(ctx)
        except Exception as exc:  # noqa: BLE001 - we classify + route every failure
            return await self._on_failure(lease, key, book_id, exc)

        # (4) Success: record terminal completion (exactly-once) + clear poison.
        self.poison.record_success(shot_id)
        self.idempotency.complete(lease, result=summary)
        return GuardResult(outcome=DurableOutcome.RENDERED, result=result)

    async def _on_failure(
        self, lease: Lease, key: IdempotencyKey, book_id: str, exc: Exception
    ) -> GuardResult:
        """Classify a render crash: retry (release claim) or quarantine + dead-letter."""
        shot_id = key.shot_id
        record = self.poison.record_failure(shot_id, exc)
        klass = classify_failure(exc)

        if record.quarantined:
            # Crossed the threshold: never re-attempt into a crash-loop. Record the
            # key completed (so re-deliveries skip) and route to the dead-letter
            # sink for triage — the render path itself still ships a bottom-rung card.
            self.idempotency.complete(
                lease, result={"dead_lettered": True, "error": record.last_error}
            )
            await self.dead_letter.dead_letter(
                shot_id=shot_id,
                book_id=book_id,
                key=key.as_str(),
                error=record.last_error or type(exc).__name__,
                failures=record.failures,
            )
            metrics.inc_render_deadletter()
            logger.error(
                "guard.dead_lettered", shot_id=shot_id, failures=record.failures, error=str(exc)
            )
            return GuardResult(outcome=DurableOutcome.DEAD_LETTERED)

        # A permanent failure that has not yet crossed the threshold still must not
        # be retried by the next delivery (it can never succeed) — leave the claim
        # held as completed-failed; a transient failure releases for a clean retry.
        if klass is FailureClass.PERMANENT:
            self.idempotency.complete(lease, result={"failed": True, "error": record.last_error})
            logger.warning("guard.permanent_failure", shot_id=shot_id, error=str(exc))
        else:
            self.idempotency.fail(lease)
            logger.warning("guard.transient_failure", shot_id=shot_id, error=str(exc))
        # Re-raise so the queue's own retry/backoff machinery still drives the
        # delivery lifecycle (the worker catches it). The claim state above governs
        # whether that retry actually re-renders.
        raise exc

    async def write_checkpoint(
        self,
        key: IdempotencyKey,
        book_id: str,
        shot_id: str,
        state: RenderState,
        *,
        attempt: int = 0,
        **fields: Any,
    ) -> None:
        """Persist a checkpoint snapshot for a shot (no-op if checkpoints disabled)."""
        if not self._checkpoint_enabled:
            return
        existing = await self.checkpoints.load(shot_id)
        base = existing or ShotCheckpoint(
            shot_id=shot_id, book_id=book_id, spec_digest=key.spec_digest
        )
        snapshot = ShotCheckpoint(
            shot_id=shot_id,
            book_id=book_id,
            state=state,
            attempts=attempt or base.attempts,
            spent_video_seconds=float(fields.get("spent_video_seconds", base.spent_video_seconds)),
            spec_digest=key.spec_digest,
            last_rung=fields.get("last_rung", base.last_rung),
            reason=fields.get("reason", base.reason),
            ledger=fields.get("ledger", base.ledger),
            revision=base.revision + 1,
        )
        await self.checkpoints.save(snapshot)
        self._emit(RenderEvent.checkpointed(shot_id, state, attempt=snapshot.attempts))
        # A terminal checkpoint lets a re-delivery short-circuit before re-rendering.
        if state in TERMINAL_STATES:
            logger.info("guard.terminal_checkpoint", shot_id=shot_id, state=state.value)

    async def clear(self, shot_id: str) -> None:
        """Drop a shot's checkpoint once it is durably terminal (housekeeping)."""
        await self.checkpoints.clear(shot_id)

    def _emit(self, event: RenderEvent) -> None:
        if self.bus is not None:
            self.bus.publish(event)
