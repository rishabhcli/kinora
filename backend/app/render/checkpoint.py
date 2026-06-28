"""Checkpoint / restore of in-flight shot renders (kinora.md §9.7, §4.11).

A per-shot render through the §9.7 state machine is long-lived (a live Wan clip
takes minutes) and side-effecting. If the render worker is restarted mid-shot,
the job is re-claimed and re-run from the top — which, without care, re-spends
video-seconds and re-writes OSS. A **checkpoint** is a small, serialisable
snapshot of a shot's progress so a resume:

* skips already-completed idempotent steps (the :class:`StepLedger` rides inside
  the checkpoint), and
* short-circuits a shot that already reached a terminal state into a no-op.

The store is a Protocol so production can persist to Redis/DB while tests use the
in-memory implementation here; both share the JSON codec so a snapshot written by
one is readable by the other. This module is pure orchestration — no ffmpeg/DB/
network — so the resume logic is unit-testable in isolation.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.logging import get_logger
from app.render.ladder import Rung
from app.render.states import TERMINAL_STATES, RenderState
from app.render.steps import StepLedger
from app.render.telemetry import TelemetryBus

logger = get_logger("app.render.checkpoint")


@dataclass(slots=True)
class ShotCheckpoint:
    """A resumable snapshot of one in-flight shot render (§9.7).

    Attributes:
        shot_id / book_id: identity.
        state: the §9.7 state the shot had reached when last checkpointed.
        attempts: repair attempts consumed so far (so resume respects the cap).
        spent_video_seconds: budget already charged (never re-charged on resume).
        spec_digest: a stable digest of the current shot spec (so resume detects
            a redesign vs the same attempt — keyed into the step ledger).
        last_rung: the ladder rung last selected (for telemetry on resume).
        reason: why the shot last checkpointed/degraded (carried for defects).
        ledger: the idempotent step ledger (completed steps skipped on resume).
        revision: monotone snapshot revision (the store keeps the latest).
    """

    shot_id: str
    book_id: str
    state: RenderState = RenderState.PROMOTED
    attempts: int = 0
    spent_video_seconds: float = 0.0
    spec_digest: str | None = None
    last_rung: Rung | None = None
    reason: str | None = None
    ledger: StepLedger | None = None
    revision: int = 0

    @property
    def is_terminal(self) -> bool:
        """True once the snapshot recorded a sink state (resume is a no-op)."""
        return self.state in TERMINAL_STATES

    def bump(self) -> ShotCheckpoint:
        """Return a copy with the revision incremented (an immutable-ish bump)."""
        return ShotCheckpoint(
            shot_id=self.shot_id,
            book_id=self.book_id,
            state=self.state,
            attempts=self.attempts,
            spent_video_seconds=self.spent_video_seconds,
            spec_digest=self.spec_digest,
            last_rung=self.last_rung,
            reason=self.reason,
            ledger=self.ledger,
            revision=self.revision + 1,
        )

    def as_dict(self) -> dict[str, Any]:
        """A JSON-serialisable snapshot (enums flattened; ledger inlined)."""
        return {
            "shot_id": self.shot_id,
            "book_id": self.book_id,
            "state": self.state.value,
            "attempts": self.attempts,
            "spent_video_seconds": self.spent_video_seconds,
            "spec_digest": self.spec_digest,
            "last_rung": self.last_rung.value if self.last_rung is not None else None,
            "reason": self.reason,
            "ledger": self.ledger.as_dict() if self.ledger is not None else None,
            "revision": self.revision,
        }

    @staticmethod
    def from_dict(data: dict[str, Any], *, bus: TelemetryBus | None = None) -> ShotCheckpoint:
        ledger_data = data.get("ledger")
        rung_raw = data.get("last_rung")
        return ShotCheckpoint(
            shot_id=str(data["shot_id"]),
            book_id=str(data["book_id"]),
            state=RenderState(str(data.get("state", RenderState.PROMOTED.value))),
            attempts=int(data.get("attempts", 0)),
            spent_video_seconds=float(data.get("spent_video_seconds", 0.0)),
            spec_digest=data.get("spec_digest"),
            last_rung=Rung(rung_raw) if rung_raw else None,
            reason=data.get("reason"),
            ledger=StepLedger.from_dict(ledger_data, bus=bus) if ledger_data else None,
            revision=int(data.get("revision", 0)),
        )


class CheckpointStore(Protocol):
    """Persist + load + clear a shot's resumable snapshot (§9.7)."""

    async def load(self, shot_id: str) -> ShotCheckpoint | None: ...

    async def save(self, checkpoint: ShotCheckpoint) -> None: ...

    async def clear(self, shot_id: str) -> None: ...


class InMemoryCheckpointStore:
    """A thread-safe in-process :class:`CheckpointStore` (the test/double impl).

    Production would swap a Redis/DB adapter behind the same Protocol; the
    in-memory store keeps a render correct *within* a process (re-claim of a job
    already finished in this worker) and is the canonical fixture for the resume
    unit tests. ``save`` keeps the highest-revision snapshot, mirroring an atomic
    compare-and-set so a stale write never clobbers a newer one.
    """

    def __init__(self) -> None:
        self._store: dict[str, ShotCheckpoint] = {}
        self._lock = threading.Lock()
        self.saves: int = 0
        self.clears: int = 0

    async def load(self, shot_id: str) -> ShotCheckpoint | None:
        with self._lock:
            return self._store.get(shot_id)

    async def save(self, checkpoint: ShotCheckpoint) -> None:
        with self._lock:
            existing = self._store.get(checkpoint.shot_id)
            if existing is not None and existing.revision > checkpoint.revision:
                return  # never let a stale snapshot clobber a newer one
            self._store[checkpoint.shot_id] = checkpoint
            self.saves += 1

    async def clear(self, shot_id: str) -> None:
        with self._lock:
            if self._store.pop(shot_id, None) is not None:
                self.clears += 1


@dataclass(slots=True)
class JsonCheckpointStore:
    """A :class:`CheckpointStore` over an injected async k/v of JSON strings.

    The adapter a real Redis client (``get``/``set``/``delete`` of JSON) drops
    into — it is exercised in tests with an in-memory dict double, so the codec
    (the §9.7 snapshot ↔ JSON contract) is verified without a live Redis.
    """

    backend: Any
    key_prefix: str = "render:checkpoint:"
    bus: TelemetryBus | None = None

    def _key(self, shot_id: str) -> str:
        return f"{self.key_prefix}{shot_id}"

    async def load(self, shot_id: str) -> ShotCheckpoint | None:
        import json

        raw = await self.backend.get(self._key(shot_id))
        if not raw:
            return None
        data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        return ShotCheckpoint.from_dict(data, bus=self.bus)

    async def save(self, checkpoint: ShotCheckpoint) -> None:
        import json

        await self.backend.set(self._key(checkpoint.shot_id), json.dumps(checkpoint.as_dict()))

    async def clear(self, shot_id: str) -> None:
        await self.backend.delete(self._key(shot_id))


@dataclass(slots=True)
class ResumeDecision:
    """The outcome of probing a checkpoint before (re)running a shot."""

    #: True when a terminal checkpoint means the render can be skipped entirely.
    skip: bool
    #: The loaded checkpoint (``None`` when there was nothing to resume from).
    checkpoint: ShotCheckpoint | None
    #: Human-readable reason, for telemetry/logs.
    reason: str


async def probe_resume(store: CheckpointStore, shot_id: str) -> ResumeDecision:
    """Decide whether a (re)claimed shot should resume, skip, or start fresh.

    * No checkpoint → start fresh (``skip=False, checkpoint=None``).
    * A terminal checkpoint → ``skip=True`` (the shot already finished; a re-claim
      after the ack/clear race must be a no-op, never a re-render).
    * A mid-flight checkpoint → ``skip=False`` with the checkpoint to resume from.
    """
    checkpoint = await store.load(shot_id)
    if checkpoint is None:
        return ResumeDecision(skip=False, checkpoint=None, reason="fresh")
    if checkpoint.is_terminal:
        logger.info(
            "checkpoint.terminal_resume_skip", shot_id=shot_id, state=checkpoint.state.value
        )
        return ResumeDecision(skip=True, checkpoint=checkpoint, reason="already_terminal")
    logger.info(
        "checkpoint.resume",
        shot_id=shot_id,
        state=checkpoint.state.value,
        attempts=checkpoint.attempts,
        steps=len(checkpoint.ledger or []),
    )
    return ResumeDecision(skip=False, checkpoint=checkpoint, reason="resume")


__all__ = [
    "CheckpointStore",
    "InMemoryCheckpointStore",
    "JsonCheckpointStore",
    "ResumeDecision",
    "ShotCheckpoint",
    "probe_resume",
]
