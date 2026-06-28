"""An idempotent, content-addressed step ledger for the render pipeline (§9.7).

A per-shot render is a sequence of expensive, side-effecting steps — reserve
budget, call Wan, score with the Critic, write the clip to OSS, populate the
cache. If a worker crashes mid-shot and the job is re-claimed, naively re-running
those steps would **double-spend video-seconds** or **double-write OSS**. This
ledger makes each step *idempotent*:

* a step has a stable ``name`` (``reserve``, ``generate``, ``qa``, …) and a
  content-addressed ``key`` derived from its inputs (e.g. the shot hash + the
  attempt's seed) — the same inputs always map to the same key;
* :meth:`StepLedger.run` runs the step's function only if ``(name, key)`` is not
  already recorded done; otherwise it returns the recorded result and emits a
  ``step_skipped`` telemetry event.

The recorded *result* is whatever the step returns; it must be cheap to retain
(an OSS key, a reservation id, a small dict) — heavy bytes belong in OSS keyed by
the same content hash, not in the ledger. The ledger is serialisable so it can
ride inside a :class:`app.render.checkpoint.ShotCheckpoint`.

This is pure orchestration: it imports no provider, DB, or ffmpeg. The step
function is injected, so tests drive it with plain callables.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from app.core.logging import get_logger
from app.render.telemetry import RenderEvent, TelemetryBus

logger = get_logger("app.render.steps")

T = TypeVar("T")


def step_key(*parts: Any) -> str:
    """A stable content-address for a step from its input ``parts``.

    Deterministic across processes/restarts (sorted, repr-stable) so the same
    inputs always resolve to the same key — the foundation of idempotency.
    """
    raw = "|".join(repr(p) for p in parts)
    return "ck1:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One completed step: its name, content key, and retained result."""

    name: str
    key: str
    result: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "key": self.key, "result": self.result}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> StepRecord:
        return StepRecord(
            name=str(data["name"]), key=str(data["key"]), result=data.get("result")
        )


@dataclass(slots=True)
class StepLedger:
    """An ordered, idempotent ledger of completed render steps for one shot.

    Keyed by ``name`` (one logical step) → its :class:`StepRecord`. A step is
    "done" only when both its name *and* content key match — so re-attempting a
    shot with a *new* seed (a new key) correctly re-runs ``generate`` rather than
    serving the stale clip, while a crash-and-resume on the *same* seed skips it.
    """

    shot_id: str
    records: dict[str, StepRecord] = field(default_factory=dict)
    bus: TelemetryBus | None = None

    def is_done(self, name: str, key: str) -> bool:
        """True iff step ``name`` was completed with this exact content ``key``."""
        record = self.records.get(name)
        return record is not None and record.key == key

    def result_of(self, name: str) -> Any:
        """The recorded result of a completed step (``None`` if not present)."""
        record = self.records.get(name)
        return record.result if record is not None else None

    def record(self, name: str, key: str, result: Any = None) -> StepRecord:
        """Record (or overwrite) a completed step."""
        rec = StepRecord(name=name, key=key, result=result)
        self.records[name] = rec
        return rec

    def forget(self, name: str) -> None:
        """Drop a step's record (so it re-runs) — used when its key changes."""
        self.records.pop(name, None)

    async def run(
        self,
        name: str,
        key: str,
        fn: Callable[[], Awaitable[T]],
        *,
        record_result: bool = True,
    ) -> T:
        """Run ``fn`` once for ``(name, key)``; on resume return the recorded result.

        When the step is already recorded done with this key, ``fn`` is **not**
        called — the recorded result is returned and a ``step_skipped`` event is
        published (so the §12.5 telemetry shows the idempotent save). Otherwise
        ``fn`` runs, its result is recorded (unless ``record_result`` is False, for
        steps whose result is too heavy to retain), and that result is returned.
        """
        if self.is_done(name, key):
            logger.info("step.skip", shot_id=self.shot_id, step=name, key=key)
            if self.bus is not None:
                self.bus.publish(RenderEvent.step_skipped(self.shot_id, name))
            return self.result_of(name)
        result = await fn()
        self.record(name, key, result if record_result else None)
        logger.info("step.done", shot_id=self.shot_id, step=name, key=key)
        return result

    def run_sync(
        self, name: str, key: str, fn: Callable[[], T], *, record_result: bool = True
    ) -> T:
        """Synchronous variant of :meth:`run` for non-async steps."""
        if self.is_done(name, key):
            if self.bus is not None:
                self.bus.publish(RenderEvent.step_skipped(self.shot_id, name))
            return self.result_of(name)
        result = fn()
        self.record(name, key, result if record_result else None)
        return result

    # -- serialisation (rides inside a checkpoint) -------------------------- #

    def as_dict(self) -> dict[str, Any]:
        return {
            "shot_id": self.shot_id,
            "records": [rec.as_dict() for rec in self.records.values()],
        }

    @staticmethod
    def from_dict(data: dict[str, Any], *, bus: TelemetryBus | None = None) -> StepLedger:
        ledger = StepLedger(shot_id=str(data["shot_id"]), bus=bus)
        for raw in data.get("records", []):
            rec = StepRecord.from_dict(raw)
            ledger.records[rec.name] = rec
        return ledger

    def __len__(self) -> int:
        return len(self.records)


# Canonical step names (so producers/consumers agree on the ledger keys).
class Step:
    """The canonical idempotent step names for a per-shot render."""

    RESERVE = "reserve"
    GENERATE = "generate"
    QA = "qa"
    PERSIST_CLIP = "persist_clip"
    PERSIST_LASTFRAME = "persist_lastframe"
    PERSIST_AUDIO = "persist_audio"
    CACHE_PUT = "cache_put"
    EPISODIC_LOG = "episodic_log"
    DEGRADE_RENDER = "degrade_render"


__all__ = [
    "Step",
    "StepLedger",
    "StepRecord",
    "step_key",
]
