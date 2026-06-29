"""The workflow **event history** — the durable, append-only source of truth.

A workflow execution *is* its event history. The workflow function is pure,
deterministic code; everything non-deterministic it does (start an activity, set
a timer, receive a signal, make a random choice, read the clock) is recorded as
an **event**. On crash-recovery the engine *replays* the same function against
the same history and the function takes exactly the same path — so a resumed run
is bit-for-bit identical to one that never crashed. This is the Temporal model.

History events come in three families:

* **Command-result events** the engine appends when it *acts on* a command the
  workflow emitted (``ActivityScheduled``, ``TimerStarted``, …) and their
  *completions* (``ActivityCompleted``, ``TimerFired``, …). During replay these
  are matched, in order, against the commands the re-run code produces — a
  mismatch is a :class:`~app.platform.workflows.errors.NonDeterminismError`.
* **External events** that arrive independently of the workflow's own commands
  (``SignalReceived``, ``WorkflowCancelRequested``). These are *injected* into the
  history and the workflow reacts to them on the next task.
* **Lifecycle events** (``WorkflowStarted``, ``WorkflowCompleted``, …) that bound
  a run.

Every event has a monotonically increasing ``event_id`` (1-based) and is fully
JSON-serialisable so it round-trips through the durable store unchanged.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.platform.workflows.serde import from_jsonable, to_jsonable


class EventType(enum.StrEnum):
    """The discriminator for a history event (stable wire strings)."""

    # --- lifecycle ---
    WORKFLOW_STARTED = "workflow_started"
    WORKFLOW_COMPLETED = "workflow_completed"
    WORKFLOW_FAILED = "workflow_failed"
    WORKFLOW_CANCELLED = "workflow_cancelled"
    WORKFLOW_CONTINUED_AS_NEW = "workflow_continued_as_new"
    WORKFLOW_TASK_STARTED = "workflow_task_started"

    # --- activities ---
    ACTIVITY_SCHEDULED = "activity_scheduled"
    ACTIVITY_COMPLETED = "activity_completed"
    ACTIVITY_FAILED = "activity_failed"
    ACTIVITY_TIMED_OUT = "activity_timed_out"
    ACTIVITY_CANCELLED = "activity_cancelled"

    # --- timers ---
    TIMER_STARTED = "timer_started"
    TIMER_FIRED = "timer_fired"
    TIMER_CANCELLED = "timer_cancelled"

    # --- signals / queries / cancel (external) ---
    SIGNAL_RECEIVED = "signal_received"
    WORKFLOW_CANCEL_REQUESTED = "workflow_cancel_requested"

    # --- child workflows ---
    CHILD_WORKFLOW_STARTED = "child_workflow_started"
    CHILD_WORKFLOW_COMPLETED = "child_workflow_completed"
    CHILD_WORKFLOW_FAILED = "child_workflow_failed"

    # --- versioning / side-effects ---
    VERSION_MARKER = "version_marker"
    SIDE_EFFECT_RECORDED = "side_effect_recorded"


@dataclass(slots=True)
class HistoryEvent:
    """One immutable entry in a workflow's event history.

    ``event_id`` is 1-based and gap-free within a run. ``timestamp`` is the
    engine's clock reading when the event was appended (used to drive durable
    timers and the workflow's deterministic ``now()``). ``attributes`` is a
    JSON-serialisable payload whose shape depends on ``type``.
    """

    event_id: int
    type: EventType
    timestamp: datetime
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain JSON-able dict (for the durable store)."""
        return {
            "event_id": self.event_id,
            "type": self.type.value,
            "timestamp": self.timestamp.isoformat(),
            "attributes": to_jsonable(self.attributes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HistoryEvent:
        """Rebuild from the durable store's JSON dict."""
        return cls(
            event_id=int(data["event_id"]),
            type=EventType(data["type"]),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            attributes=from_jsonable(data.get("attributes", {})),
        )


def attr(event: HistoryEvent, key: str, default: Any = None) -> Any:
    """Typed-ish accessor for an event attribute (keeps call sites tidy)."""
    return event.attributes.get(key, default)


__all__ = ["EventType", "HistoryEvent", "attr"]
