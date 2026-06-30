"""Crash-recovery + exactly-once hardening for the §9.7 render machine.

The render pipeline (:mod:`app.render.pipeline`) is correct on the happy path, but
its queue is at-least-once and a worker can crash mid-shot. This package makes a
per-shot render **safe to deliver more than once and safe to interrupt**, composing
the existing durability primitives (:mod:`app.render.checkpoint`,
:mod:`app.render.steps`, :mod:`app.render.poison`) into one orchestration layer:

* :mod:`~app.render.durability.keys` — the content identity ``(shot_id, spec
  digest)`` every other layer keys off (a redesign is a new render, a re-delivery
  is the same one).
* :mod:`~app.render.durability.idempotency` — admit at-most-one live render per
  key; a duplicate delivery defers (in-flight) or serves the recorded result
  (completed). No double-render, no double-spend.
* :mod:`~app.render.durability.commit` — persist an accepted clip exactly once,
  dedup on retry, inside an atomic transaction boundary.
* :mod:`~app.render.durability.deadletter` — route a crash-loop (poison) shot to a
  triage sink while the film still ships its bottom-rung card.
* :mod:`~app.render.durability.guard` — :class:`DurableRenderGuard`: the wrapper
  that ties checkpoint + idempotency + poison + dead-letter around a render call.
* :mod:`~app.render.durability.recovery` — a startup/long-running loop (mirroring
  :mod:`app.ingest.recovery`) that resumes or repairs shots stuck non-terminal.
* :mod:`~app.render.durability.repository` — production adapter sketches (Redis
  claims, the Postgres stuck-shot scan) behind the same Protocols.

Every seam has an in-memory implementation, so the whole subsystem is unit-testable
with no infra and no spend.
"""

from __future__ import annotations

from app.render.durability.commit import (
    AcceptedClipRecord,
    ClipCommitter,
    CommitLog,
    CommitTransaction,
    InMemoryCommitLog,
    NullCommitTransaction,
)
from app.render.durability.deadletter import (
    DeadLetterEntry,
    DeadLetterSink,
    InMemoryDeadLetterSink,
    NullDeadLetterSink,
    RepoDeadLetterSink,
)
from app.render.durability.guard import (
    DurableOutcome,
    DurableRenderGuard,
    GuardResult,
    RenderCall,
    ResumeContext,
)
from app.render.durability.idempotency import (
    Admission,
    AdmissionResult,
    ClaimRecord,
    ClaimState,
    IdempotencyGuard,
    IdempotencyStore,
    InMemoryIdempotencyStore,
    Lease,
)
from app.render.durability.keys import IdempotencyKey, spec_digest
from app.render.durability.recovery import (
    NON_TERMINAL_STATES,
    RecoveryAction,
    RecoveryReport,
    ShotRecoveryService,
    StuckShot,
    StuckShotRepo,
    run_recovery_loop,
)

__all__ = [
    "NON_TERMINAL_STATES",
    "AcceptedClipRecord",
    "Admission",
    "AdmissionResult",
    "ClaimRecord",
    "ClaimState",
    "ClipCommitter",
    "CommitLog",
    "CommitTransaction",
    "DeadLetterEntry",
    "DeadLetterSink",
    "DurableOutcome",
    "DurableRenderGuard",
    "GuardResult",
    "IdempotencyGuard",
    "IdempotencyKey",
    "IdempotencyStore",
    "InMemoryCommitLog",
    "InMemoryDeadLetterSink",
    "InMemoryIdempotencyStore",
    "Lease",
    "NullCommitTransaction",
    "NullDeadLetterSink",
    "RecoveryAction",
    "RecoveryReport",
    "RenderCall",
    "RepoDeadLetterSink",
    "ResumeContext",
    "ShotRecoveryService",
    "StuckShot",
    "StuckShotRepo",
    "run_recovery_loop",
    "spec_digest",
]
