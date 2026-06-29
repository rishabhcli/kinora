"""Commands — the workflow's outbound intent during a single task.

When the engine runs (or replays) a workflow task, the workflow code does not
*perform* side effects directly. Instead it **emits commands**: "schedule this
activity", "start this timer", "complete with this result". The executor
collects the commands produced during the task, then:

* during **fresh** execution, applies each command by appending the matching
  history event(s) and dispatching real work (enqueue the activity task, arm the
  timer);
* during **replay**, *matches* each command, in order, against the already
  recorded events to re-hydrate futures — appending nothing and dispatching
  nothing. A command that doesn't line up with the next recorded event is a
  non-determinism error.

This command/event split is the core of the deterministic-replay model: commands
are the *only* way workflow code reaches the outside world, and every command has
a corresponding recorded event, so the path is fully reconstructable.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from app.platform.workflows.retry import RetryPolicy


class CommandType(enum.StrEnum):
    """Discriminator for a command emitted during a workflow task."""

    SCHEDULE_ACTIVITY = "schedule_activity"
    START_TIMER = "start_timer"
    CANCEL_TIMER = "cancel_timer"
    START_CHILD_WORKFLOW = "start_child_workflow"
    RECORD_VERSION_MARKER = "record_version_marker"
    RECORD_SIDE_EFFECT = "record_side_effect"
    COMPLETE_WORKFLOW = "complete_workflow"
    FAIL_WORKFLOW = "fail_workflow"
    CONTINUE_AS_NEW = "continue_as_new"


@dataclass(slots=True)
class Command:
    """Base command. ``seq`` is the workflow-local sequence number that ties a
    command to the future the workflow code is awaiting (the executor assigns it
    monotonically per task-run from the workflow's own deterministic counter)."""

    type: CommandType
    seq: int


@dataclass(slots=True)
class ScheduleActivity(Command):
    """Schedule an activity execution."""

    activity_type: str = ""
    args: list[Any] = field(default_factory=list)
    kwargs: dict[str, Any] = field(default_factory=dict)
    task_queue: str | None = None
    retry_policy: RetryPolicy | None = None
    start_to_close_timeout_s: float | None = None
    schedule_to_close_timeout_s: float | None = None
    heartbeat_timeout_s: float | None = None


@dataclass(slots=True)
class StartTimer(Command):
    """Arm a durable timer that fires after ``delay_s`` of workflow time."""

    delay_s: float = 0.0


@dataclass(slots=True)
class CancelTimer(Command):
    """Cancel a previously started timer by its start ``seq``."""

    timer_seq: int = 0


@dataclass(slots=True)
class StartChildWorkflow(Command):
    """Start a child workflow execution."""

    workflow_type: str = ""
    child_workflow_id: str = ""
    args: list[Any] = field(default_factory=list)
    kwargs: dict[str, Any] = field(default_factory=dict)
    task_queue: str | None = None


@dataclass(slots=True)
class RecordVersionMarker(Command):
    """Pin a code-version branch decision so replay stays deterministic."""

    change_id: str = ""
    version: int = 0


@dataclass(slots=True)
class RecordSideEffect(Command):
    """Memoise a non-deterministic local computation's result into history."""

    value: Any = None


@dataclass(slots=True)
class CompleteWorkflow(Command):
    """Finish the workflow successfully with ``result``."""

    result: Any = None


@dataclass(slots=True)
class FailWorkflow(Command):
    """Finish the workflow as failed."""

    error: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ContinueAsNew(Command):
    """Atomically close this run and start a fresh one (same id, empty history)."""

    args: list[Any] = field(default_factory=list)
    kwargs: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "CancelTimer",
    "Command",
    "CommandType",
    "CompleteWorkflow",
    "ContinueAsNew",
    "FailWorkflow",
    "RecordSideEffect",
    "RecordVersionMarker",
    "ScheduleActivity",
    "StartChildWorkflow",
    "StartTimer",
]
