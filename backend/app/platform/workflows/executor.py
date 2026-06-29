"""The workflow **executor** — turn a workflow task into history mutations + work.

:func:`execute_workflow_task` is the single function that advances a workflow by
exactly one *task*:

1. Build a :class:`ReplayState` by scanning the persisted history.
2. Instantiate the workflow function with a fresh :class:`WorkflowContext` and run
   its coroutine (:func:`run_workflow_coroutine`) — replay re-derives all prior
   commands silently (their futures are resolved from history), then the code
   either *advances* (emits new commands) or *parks* (suspends on an unresolved
   future) or *finishes* (returns / raises).
3. Match the emitted commands against the unprocessed command-result events in
   history to detect non-determinism, then translate the *new* (unmatched-tail)
   commands into the side effects the engine must perform:
   * append the scheduling events (``ACTIVITY_SCHEDULED``, ``TIMER_STARTED``, …),
   * hand back **dispatch instructions** (activity tasks to enqueue, timers to
     arm, child workflows to start) for the worker runtime to act on,
   * record completion/failure/continue-as-new lifecycle events.

The executor never performs I/O itself (it's pure given the history): it appends
events and returns a :class:`WorkflowTaskOutcome` describing what to dispatch. The
worker runtime (:mod:`app.platform.workflows.worker`) owns the actual enqueueing,
which keeps the executor unit-testable with zero infra and the determinism
guarantees crisp.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.platform.workflows.commands import (
    CancelTimer,
    Command,
    CommandType,
    CompleteWorkflow,
    ContinueAsNew,
    FailWorkflow,
    RecordSideEffect,
    RecordVersionMarker,
    ScheduleActivity,
    StartChildWorkflow,
    StartTimer,
)
from app.platform.workflows.context import WorkflowContext, WorkflowInfo, _ContinueAsNewSignal
from app.platform.workflows.errors import ApplicationError, NonDeterminismError
from app.platform.workflows.events import EventType, HistoryEvent, attr
from app.platform.workflows.registry import WorkflowDefinition
from app.platform.workflows.replay import build_replay_state, run_workflow_coroutine

# History event type produced for each *matched* scheduling command (the
# determinism map). Memoised commands (version marker, side-effect) are NOT here:
# they re-emit no command on replay, so they're never matched (see
# ``_RECORDED_COMMAND_EVENTS`` and :func:`_match_against_history`).
_SCHEDULE_EVENT_FOR: dict[CommandType, EventType] = {
    CommandType.SCHEDULE_ACTIVITY: EventType.ACTIVITY_SCHEDULED,
    CommandType.START_TIMER: EventType.TIMER_STARTED,
    CommandType.START_CHILD_WORKFLOW: EventType.CHILD_WORKFLOW_STARTED,
}


@dataclass(slots=True)
class ActivityDispatch:
    """An activity the worker runtime must enqueue onto a task queue."""

    seq: int
    activity_type: str
    args: list[Any]
    kwargs: dict[str, Any]
    task_queue: str | None
    retry_policy_dict: dict[str, Any] | None
    start_to_close_timeout_s: float | None
    schedule_to_close_timeout_s: float | None
    heartbeat_timeout_s: float | None


@dataclass(slots=True)
class TimerDispatch:
    """A durable timer the runtime must arm (fires at ``fire_at`` workflow time)."""

    seq: int
    fire_at: datetime


@dataclass(slots=True)
class TimerCancelDispatch:
    """A pending timer the runtime must disarm."""

    timer_seq: int


@dataclass(slots=True)
class ChildWorkflowDispatch:
    """A child workflow the runtime must start."""

    seq: int
    workflow_type: str
    child_workflow_id: str
    args: list[Any]
    kwargs: dict[str, Any]
    task_queue: str | None


@dataclass(slots=True)
class WorkflowTaskOutcome:
    """The full result of one executed workflow task."""

    new_events: list[HistoryEvent]
    activities: list[ActivityDispatch] = field(default_factory=list)
    timers: list[TimerDispatch] = field(default_factory=list)
    timer_cancels: list[TimerCancelDispatch] = field(default_factory=list)
    children: list[ChildWorkflowDispatch] = field(default_factory=list)
    completed: bool = False
    result: Any = None
    failed: bool = False
    error: ApplicationError | None = None
    continued_as_new: bool = False
    continue_args: list[Any] = field(default_factory=list)
    continue_kwargs: dict[str, Any] = field(default_factory=dict)

    @property
    def made_progress(self) -> bool:
        """Did the task append anything / finish? (False → workflow merely parked.)"""
        return bool(self.new_events) or self.completed or self.failed or self.continued_as_new


def execute_workflow_task(
    *,
    definition: WorkflowDefinition,
    history: list[HistoryEvent],
    info: WorkflowInfo,
    workflow_args: list[Any],
    workflow_kwargs: dict[str, Any],
    now: datetime,
) -> WorkflowTaskOutcome:
    """Advance the workflow one task; append history events; return dispatches.

    If the history already contains a terminal lifecycle event the run is finished;
    re-running the body would re-derive its commands and (because completion is
    synthesised, not commanded) try to re-complete it. We short-circuit to a
    no-progress outcome — this is what makes :func:`execute_workflow_task` a safe
    fixed-point check for the deterministic-replay assertion.
    """
    if any(e.type in _TERMINAL_EVENTS for e in history):
        return WorkflowTaskOutcome(new_events=[])

    state = build_replay_state(history)
    ctx = WorkflowContext(state, info)

    # Run the workflow body. Replay re-derives prior commands; new ones append.
    coro = definition.fn(ctx, *workflow_args, **workflow_kwargs)
    finished = False
    result: Any = None
    failed = False
    app_error: ApplicationError | None = None
    continued = False
    try:
        finished, result = run_workflow_coroutine(coro)
    except _ContinueAsNewSignal:
        continued = True
    except ApplicationError as exc:
        failed, app_error = True, exc
    except NonDeterminismError:
        raise
    except Exception as exc:  # noqa: BLE001 - workflow code raised; fail the run
        failed, app_error = True, ApplicationError(str(exc), type=type(exc).__name__)

    commands = ctx.commands
    next_event_id = (history[-1].event_id if history else 0) + 1

    # Partition commands into (already in history → validate) and (new → apply),
    # keyed by the command's seq. Seq-keyed (not positional) matching is robust to
    # memoised reads — ``side_effect`` / ``get_version`` re-emit no command on
    # replay yet still advance the seq counter, so a positional scan would skew.
    new_commands = _match_against_history(commands, history)

    outcome = WorkflowTaskOutcome(new_events=[])
    event_id = next_event_id
    for cmd in new_commands:
        event_id = _apply_command(cmd, outcome, event_id, now, info)

    # Terminal handling: a fresh COMPLETE/FAIL/CONTINUE command was emitted, or
    # the coroutine finished/raised without one (the executor synthesises it).
    if finished and not _has_terminal_command(new_commands):
        outcome.new_events.append(
            HistoryEvent(event_id, EventType.WORKFLOW_COMPLETED, now, {"result": result})
        )
        outcome.completed = True
        outcome.result = result
    elif failed and app_error is not None:
        outcome.new_events.append(
            HistoryEvent(
                event_id, EventType.WORKFLOW_FAILED, now, {"error": _error_dict(app_error)}
            )
        )
        outcome.failed = True
        outcome.error = app_error
    elif continued:
        # The ContinueAsNew command was already turned into an event below.
        outcome.continued_as_new = True

    return outcome


def _match_against_history(commands: list[Command], history: list[HistoryEvent]) -> list[Command]:
    """Validate already-recorded commands and return the *new* (unrecorded) ones.

    Builds a ``seq → event`` index over the recorded command-events, then walks the
    freshly-produced commands in order:

    * a command whose seq is **in** the index must match that event
      (:func:`_assert_command_matches_event`) — a divergence is non-determinism;
    * a command whose seq is **not** in the index is *new* (the workflow advanced
      past the recorded frontier) and is returned for application.

    A recorded command-event whose seq the re-run code never reproduced is also
    non-determinism (the code dropped a step), caught at the end.
    """
    recorded_by_seq: dict[int, HistoryEvent] = {
        attr(e, "seq"): e for e in history if e.type in _RECORDED_COMMAND_EVENTS
    }
    matched_seqs: set[int] = set()
    new_commands: list[Command] = []
    for cmd in commands:
        event = recorded_by_seq.get(cmd.seq)
        if event is None:
            new_commands.append(cmd)
            continue
        _assert_command_matches_event(cmd, event)
        matched_seqs.add(cmd.seq)
    missing = set(recorded_by_seq) - matched_seqs
    if missing:
        seq = min(missing)
        event = recorded_by_seq[seq]
        raise NonDeterminismError(
            f"history event {event.type} (seq {seq}) has no matching command on "
            "replay — workflow code changed incompatibly",
            expected=event.type.value,
            actual=None,
        )
    return new_commands


def _assert_command_matches_event(cmd: Command, event: HistoryEvent) -> None:
    """Raise if ``cmd`` doesn't correspond to the recorded ``event``."""
    expected_type = _SCHEDULE_EVENT_FOR.get(cmd.type)
    if expected_type is None:
        # CANCEL_TIMER is the only matched command without a scheduling-event map.
        expected_type = EventType.TIMER_CANCELLED if cmd.type is CommandType.CANCEL_TIMER else None
    if event.type is not expected_type:
        raise NonDeterminismError(
            f"replay mismatch at seq {cmd.seq}: code emitted {cmd.type} but history "
            f"has {event.type}",
            expected=event.type.value,
            actual=cmd.type.value,
        )
    if attr(event, "seq") != cmd.seq:
        raise NonDeterminismError(
            f"replay mismatch: command seq {cmd.seq} != recorded seq {attr(event, 'seq')} "
            f"for {cmd.type}",
            expected=attr(event, "seq"),
            actual=cmd.seq,
        )
    # For activities, the call target must also match (defends against reordered
    # or renamed activity calls that would otherwise silently re-bind a result).
    if isinstance(cmd, ScheduleActivity):
        recorded_type = attr(event, "activity_type")
        if recorded_type != cmd.activity_type:
            raise NonDeterminismError(
                f"replay mismatch at seq {cmd.seq}: activity {cmd.activity_type!r} != "
                f"recorded {recorded_type!r}",
                expected=recorded_type,
                actual=cmd.activity_type,
            )


def _apply_command(
    cmd: Command,
    outcome: WorkflowTaskOutcome,
    event_id: int,
    now: datetime,
    info: WorkflowInfo,
) -> int:
    """Append the event(s) for a *new* command and record its dispatch. Returns
    the next event id."""
    if isinstance(cmd, ScheduleActivity):
        outcome.new_events.append(
            HistoryEvent(
                event_id,
                EventType.ACTIVITY_SCHEDULED,
                now,
                {
                    "seq": cmd.seq,
                    "activity_type": cmd.activity_type,
                    "args": cmd.args,
                    "kwargs": cmd.kwargs,
                    "task_queue": cmd.task_queue,
                },
            )
        )
        outcome.activities.append(
            ActivityDispatch(
                seq=cmd.seq,
                activity_type=cmd.activity_type,
                args=cmd.args,
                kwargs=cmd.kwargs,
                task_queue=cmd.task_queue,
                retry_policy_dict=cmd.retry_policy.to_dict() if cmd.retry_policy else None,
                start_to_close_timeout_s=cmd.start_to_close_timeout_s,
                schedule_to_close_timeout_s=cmd.schedule_to_close_timeout_s,
                heartbeat_timeout_s=cmd.heartbeat_timeout_s,
            )
        )
        return event_id + 1
    if isinstance(cmd, StartTimer):
        fire_at = now + timedelta(seconds=cmd.delay_s)
        outcome.new_events.append(
            HistoryEvent(
                event_id,
                EventType.TIMER_STARTED,
                now,
                {"seq": cmd.seq, "delay_s": cmd.delay_s, "fire_at": fire_at.isoformat()},
            )
        )
        outcome.timers.append(TimerDispatch(seq=cmd.seq, fire_at=fire_at))
        return event_id + 1
    if isinstance(cmd, CancelTimer):
        outcome.new_events.append(
            HistoryEvent(
                event_id,
                EventType.TIMER_CANCELLED,
                now,
                {"seq": cmd.seq, "timer_seq": cmd.timer_seq},
            )
        )
        outcome.timer_cancels.append(TimerCancelDispatch(timer_seq=cmd.timer_seq))
        return event_id + 1
    if isinstance(cmd, StartChildWorkflow):
        outcome.new_events.append(
            HistoryEvent(
                event_id,
                EventType.CHILD_WORKFLOW_STARTED,
                now,
                {
                    "seq": cmd.seq,
                    "workflow_type": cmd.workflow_type,
                    "child_workflow_id": cmd.child_workflow_id,
                    "args": cmd.args,
                    "kwargs": cmd.kwargs,
                    "task_queue": cmd.task_queue,
                },
            )
        )
        outcome.children.append(
            ChildWorkflowDispatch(
                seq=cmd.seq,
                workflow_type=cmd.workflow_type,
                child_workflow_id=cmd.child_workflow_id,
                args=cmd.args,
                kwargs=cmd.kwargs,
                task_queue=cmd.task_queue,
            )
        )
        return event_id + 1
    if isinstance(cmd, RecordVersionMarker):
        outcome.new_events.append(
            HistoryEvent(
                event_id,
                EventType.VERSION_MARKER,
                now,
                {"seq": cmd.seq, "change_id": cmd.change_id, "version": cmd.version},
            )
        )
        return event_id + 1
    if isinstance(cmd, RecordSideEffect):
        outcome.new_events.append(
            HistoryEvent(
                event_id, EventType.SIDE_EFFECT_RECORDED, now, {"seq": cmd.seq, "value": cmd.value}
            )
        )
        return event_id + 1
    if isinstance(cmd, CompleteWorkflow):
        outcome.new_events.append(
            HistoryEvent(event_id, EventType.WORKFLOW_COMPLETED, now, {"result": cmd.result})
        )
        outcome.completed = True
        outcome.result = cmd.result
        return event_id + 1
    if isinstance(cmd, FailWorkflow):
        outcome.new_events.append(
            HistoryEvent(event_id, EventType.WORKFLOW_FAILED, now, {"error": cmd.error})
        )
        outcome.failed = True
        outcome.error = ApplicationError(
            cmd.error.get("message", "workflow failed"), type=cmd.error.get("type")
        )
        return event_id + 1
    if isinstance(cmd, ContinueAsNew):
        outcome.new_events.append(
            HistoryEvent(
                event_id,
                EventType.WORKFLOW_CONTINUED_AS_NEW,
                now,
                {"args": cmd.args, "kwargs": cmd.kwargs},
            )
        )
        outcome.continued_as_new = True
        outcome.continue_args = cmd.args
        outcome.continue_kwargs = cmd.kwargs
        return event_id + 1
    raise NonDeterminismError(f"unknown command type: {cmd.type}")


def _has_terminal_command(commands: list[Command]) -> bool:
    return any(
        c.type
        in (CommandType.COMPLETE_WORKFLOW, CommandType.FAIL_WORKFLOW, CommandType.CONTINUE_AS_NEW)
        for c in commands
    )


def _error_dict(error: ApplicationError) -> dict[str, Any]:
    return {
        "message": error.message,
        "type": error.type,
        "non_retryable": error.non_retryable,
        "details": error.details,
    }


#: History events that represent a command the workflow code **always re-emits**
#: on replay (the determinism-matching set). Excluded by design:
#:
#: * Terminal lifecycle events (``WORKFLOW_COMPLETED``/``FAILED``/
#:   ``CONTINUED_AS_NEW``) are *synthesised by the executor*, not commanded by
#:   ordinary workflow code; a replay of a finished run short-circuits before any
#:   matching (see :func:`execute_workflow_task`).
#: * ``VERSION_MARKER`` and ``SIDE_EFFECT_RECORDED`` are *memoised reads*: on the
#:   first execution they emit a command (and are appended), but on replay the
#:   value is read straight from history and **no command is re-emitted** — so
#:   they must not participate in command↔event matching (their determinism is
#:   guaranteed instead by the recorded value the workflow reads back).
#:
#: That leaves the commands that genuinely re-issue every task: activities,
#: timers, timer-cancels, and child workflows.
_RECORDED_COMMAND_EVENTS = frozenset(
    {
        EventType.ACTIVITY_SCHEDULED,
        EventType.TIMER_STARTED,
        EventType.TIMER_CANCELLED,
        EventType.CHILD_WORKFLOW_STARTED,
    }
)

#: Terminal lifecycle events; their presence means the run is already finished.
_TERMINAL_EVENTS = frozenset(
    {
        EventType.WORKFLOW_COMPLETED,
        EventType.WORKFLOW_FAILED,
        EventType.WORKFLOW_CANCELLED,
        EventType.WORKFLOW_CONTINUED_AS_NEW,
    }
)


__all__ = [
    "ActivityDispatch",
    "ChildWorkflowDispatch",
    "TimerCancelDispatch",
    "TimerDispatch",
    "WorkflowTaskOutcome",
    "execute_workflow_task",
]
