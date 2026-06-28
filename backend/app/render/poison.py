"""Dead-shot / poison handling for the render engine (§4.11, §12.1).

A *clean* degrade (QA fail → Ken-Burns) is the design working as intended. A
**poison** shot is different: one that repeatedly *crashes* the renderer itself —
a malformed beat, a span that makes the Cinematographer throw, an asset that
breaks ffmpeg — and would otherwise be re-claimed and re-crash forever, wedging
its lane and (if it ever reached a live render) bleeding budget in a crash-loop.

This module counts a shot's *hard* failures across attempts and restarts and,
once it crosses a deterministic threshold, **quarantines** it:

* the shot is forced to the bottom rung (the audio-text card — guaranteed to
  render with no assets or providers), so the film still never hard-stops; and
* a ``poison`` defect is logged so the shot is visible for human triage.

The tracker is the durable memory of "this shot keeps blowing up"; production can
persist its counters behind the :class:`PoisonStore` Protocol (Redis/DB) so the
quarantine survives a worker restart — the in-memory store here is the test/double
and the within-process correctness guarantee. Pure orchestration; no ffmpeg/DB.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.logging import get_logger
from app.observability import metrics
from app.render.ladder import LadderReason, Rung
from app.render.retry import FailureClass, classify_failure
from app.render.telemetry import RenderEvent, TelemetryBus

logger = get_logger("app.render.poison")


@dataclass(slots=True)
class PoisonRecord:
    """A shot's accumulated hard-failure history."""

    shot_id: str
    failures: int = 0
    last_error: str | None = None
    quarantined: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "shot_id": self.shot_id,
            "failures": self.failures,
            "last_error": self.last_error,
            "quarantined": self.quarantined,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> PoisonRecord:
        return PoisonRecord(
            shot_id=str(data["shot_id"]),
            failures=int(data.get("failures", 0)),
            last_error=data.get("last_error"),
            quarantined=bool(data.get("quarantined", False)),
        )


class PoisonStore(Protocol):
    """Persist + load a shot's poison record (durable quarantine across restarts)."""

    def get(self, shot_id: str) -> PoisonRecord | None: ...

    def put(self, record: PoisonRecord) -> None: ...

    def clear(self, shot_id: str) -> None: ...


class InMemoryPoisonStore:
    """A thread-safe in-process :class:`PoisonStore` (the test/double + within-proc impl)."""

    def __init__(self) -> None:
        self._store: dict[str, PoisonRecord] = {}
        self._lock = threading.Lock()

    def get(self, shot_id: str) -> PoisonRecord | None:
        with self._lock:
            record = self._store.get(shot_id)
            # Return a copy so callers can't mutate the stored record in place.
            return PoisonRecord.from_dict(record.as_dict()) if record is not None else None

    def put(self, record: PoisonRecord) -> None:
        with self._lock:
            self._store[record.shot_id] = PoisonRecord.from_dict(record.as_dict())

    def clear(self, shot_id: str) -> None:
        with self._lock:
            self._store.pop(shot_id, None)


@dataclass(slots=True)
class PoisonTracker:
    """Counts hard render failures per shot and quarantines a crash-loop.

    Attributes:
        store: where the per-shot counters live (durable in production).
        threshold: hard failures before quarantine (default from settings:
            ``render_poison_threshold``).
        bus: optional telemetry bus to publish a ``poisoned`` event on quarantine.
    """

    store: PoisonStore = field(default_factory=InMemoryPoisonStore)
    threshold: int = 3
    bus: TelemetryBus | None = None

    def is_poisoned(self, shot_id: str) -> bool:
        """True when a shot is already quarantined (force the bottom rung)."""
        record = self.store.get(shot_id)
        return record is not None and record.quarantined

    def failures(self, shot_id: str) -> int:
        """How many hard failures a shot has accrued."""
        record = self.store.get(shot_id)
        return record.failures if record is not None else 0

    def record_success(self, shot_id: str) -> None:
        """Clear a shot's poison history once it finally renders cleanly."""
        self.store.clear(shot_id)

    def record_failure(self, shot_id: str, exc: BaseException) -> PoisonRecord:
        """Count one *hard* failure and quarantine the shot if it crosses the cap.

        A permanent failure (one that can never succeed) counts double-weight so a
        deterministically-broken shot is quarantined faster than a flapping one.
        Publishes a ``poisoned`` telemetry event + bumps the additive metric on the
        transition into quarantine (once, not every subsequent failure).
        """
        record = self.store.get(shot_id) or PoisonRecord(shot_id=shot_id)
        weight = 2 if classify_failure(exc) is FailureClass.PERMANENT else 1
        record.failures += weight
        record.last_error = type(exc).__name__
        newly_quarantined = not record.quarantined and record.failures >= self.threshold
        if newly_quarantined:
            record.quarantined = True
        self.store.put(record)
        logger.warning(
            "poison.failure",
            shot_id=shot_id,
            failures=record.failures,
            error=record.last_error,
            quarantined=record.quarantined,
        )
        if newly_quarantined:
            metrics.inc_render_poison()
            if self.bus is not None:
                self.bus.publish(
                    RenderEvent.poisoned(
                        shot_id, failures=record.failures, reason=record.last_error or "crash"
                    )
                )
            logger.error("poison.quarantined", shot_id=shot_id, failures=record.failures)
        return record

    def quarantine_plan_input(self, shot_id: str) -> tuple[Rung, LadderReason] | None:
        """The forced rung + reason for a quarantined shot, or ``None`` if clean.

        A quarantined shot is forced to the guaranteed-renderable bottom rung
        (audio-text card) with the ``POISONED`` reason — so even a shot that
        crashes every richer lane still ships a playable card (§4.11).
        """
        if not self.is_poisoned(shot_id):
            return None
        return Rung.AUDIO_TEXT_ONLY, LadderReason.POISONED


__all__ = [
    "InMemoryPoisonStore",
    "PoisonRecord",
    "PoisonStore",
    "PoisonTracker",
]
