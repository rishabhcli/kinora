"""The replay core — drive a workflow coroutine against its event history.

This is the engine's beating heart. Given a workflow function and an event
history, :class:`ReplayState` + :func:`replay_workflow_task` reconstruct the
workflow's in-memory state by:

1. **Indexing history** into resolved futures, recorded versions/side-effects,
   delivered signals, and the cancel flag — everything the workflow code will
   look up.
2. **Running the workflow coroutine** by hand (no event loop). Each ``await`` of
   an engine future either returns its history-resolved value or raises
   :class:`WorkflowSuspended` (no completion yet → the workflow parks).
3. **Collecting the commands** the code emitted this task and **matching** them,
   in order, against the *unprocessed* command-result events already in history —
   any divergence is a :class:`NonDeterminismError`. Commands with no matching
   history event are *new* (the workflow advanced) and get returned to the
   executor to be applied (append events + dispatch real work).

The crucial invariant: **the same history always yields the same command stream**
up to the point new commands begin. That is what makes "resume after a crash" ≡
"run that never crashed": replay reconstructs identical state, then continues.

The seq numbering is the linchpin. Every history-recording call in the context
(``execute_activity``, ``start_timer``, …, ``uuid4``, ``side_effect``,
``get_version``) draws a monotonically increasing ``seq`` from :meth:`next_seq`.
Because the workflow is deterministic, it draws the *same* seq for the *same*
logical step on every replay, so a completion event recorded against ``seq=k``
re-hydrates exactly the future the re-run code creates at ``seq=k``.
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.platform.workflows.commands import Command
from app.platform.workflows.errors import (
    ActivityCancelled,
    ActivityFailure,
    ActivityTimeout,
    ApplicationError,
    ChildWorkflowFailure,
    NonDeterminismError,
    WorkflowSuspended,
)
from app.platform.workflows.events import EventType, HistoryEvent, attr
from app.platform.workflows.futures import WorkflowFuture

#: Sentinel: a side-effect seq has no recorded value yet.
_NO_SIDE_EFFECT = object()


@dataclass(slots=True)
class _Completion:
    """A resolved outcome pulled from history for a given command seq."""

    value: Any = None
    exception: BaseException | None = None


class ReplayState:
    """The reconstructed, in-memory state a :class:`WorkflowContext` reads from.

    Built once per workflow task by scanning the history. It owns:

    * ``_completions`` — seq → resolved result/exception (activities, timers,
      child workflows) so the matching :class:`WorkflowFuture` resolves on await;
    * ``_versions`` — change_id → pinned version (from ``VERSION_MARKER`` events);
    * ``_side_effects`` — seq → recorded side-effect value;
    * ``_signals`` — name → list of delivered payloads + a per-name consume cursor;
    * ``cancel_requested`` — whether a cancel event is present;
    * ``current_time`` — the timestamp the deterministic clock reports.

    It also assigns the per-task command sequence numbers via :meth:`next_seq`.
    """

    def __init__(self, *, current_time: datetime) -> None:
        self._completions: dict[int, _Completion] = {}
        self._versions: dict[str, int] = {}
        self._side_effects: dict[int, Any] = {}
        self._signals: dict[str, list[Any]] = {}
        self._signal_cursor: dict[str, int] = {}
        self.cancel_requested = False
        self.current_time = current_time
        #: How many recorded command-events (activity/timer/child schedules) the
        #: history holds — the frontier marker for versioning (a body that has
        #: re-emitted this many scheduling commands has caught up to live code).
        self.recorded_command_event_count = 0
        self._seq = 0
        # The unprocessed command-result events to match commands against this
        # task (populated by the executor; consumed in command order).

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def future_for(self, seq: int) -> WorkflowFuture[Any]:
        """A future pre-resolved from history if a completion exists for ``seq``."""
        fut: WorkflowFuture[Any] = WorkflowFuture(seq)
        completion = self._completions.get(seq)
        if completion is not None:
            if completion.exception is not None:
                fut.set_exception(completion.exception)
            else:
                fut.set_result(completion.value)
        return fut

    def recorded_version(self, change_id: str) -> int | None:
        return self._versions.get(change_id)

    def recorded_side_effect(self, seq: int) -> Any:
        return self._side_effects.get(seq, _NO_SIDE_EFFECT)

    @staticmethod
    def is_no_side_effect(value: Any) -> bool:
        """True when ``value`` is the 'not recorded yet' sentinel (identity check)."""
        return value is _NO_SIDE_EFFECT

    def signals_for(self, name: str) -> list[Any]:
        return list(self._signals.get(name, ()))

    def signal_future(self, name: str) -> WorkflowFuture[Any]:
        """Resolve with the next *unconsumed* payload for ``name`` (else suspend)."""
        delivered = self._signals.get(name, [])
        cursor = self._signal_cursor.get(name, 0)
        fut: WorkflowFuture[Any] = WorkflowFuture(-1)
        if cursor < len(delivered):
            self._signal_cursor[name] = cursor + 1
            fut.set_result(delivered[cursor])
        return fut

    # ----- history indexing --------------------------------------------------
    def index_completion(self, seq: int, completion: _Completion) -> None:
        self._completions[seq] = completion

    def index_version(self, change_id: str, version: int) -> None:
        self._versions[change_id] = version

    def index_side_effect(self, seq: int, value: Any) -> None:
        self._side_effects[seq] = value

    def deliver_signal(self, name: str, payload: Any) -> None:
        self._signals.setdefault(name, []).append(payload)


def build_replay_state(history: list[HistoryEvent]) -> ReplayState:
    """Scan a full history into a :class:`ReplayState` ready for a workflow task.

    The mapping from a *scheduling* event (``ACTIVITY_SCHEDULED`` at ``seq=k``) to
    its *completion* (``ACTIVITY_COMPLETED`` carrying ``seq=k``) is what resolves
    futures. Timers, child workflows, side-effects, versions, signals, and the
    cancel flag are indexed the same way.
    """
    current_time = history[0].timestamp if history else _epoch()
    state = ReplayState(current_time=current_time)
    for event in history:
        current_time = event.timestamp
        et = event.type
        if et is EventType.ACTIVITY_COMPLETED:
            state.index_completion(attr(event, "seq"), _Completion(value=attr(event, "result")))
        elif et is EventType.ACTIVITY_FAILED:
            cause = _app_error_from(attr(event, "error", {}))
            state.index_completion(
                attr(event, "seq"),
                _Completion(
                    exception=ActivityFailure(attr(event, "activity_type", "?"), cause=cause)
                ),
            )
        elif et is EventType.ACTIVITY_TIMED_OUT:
            state.index_completion(
                attr(event, "seq"),
                _Completion(
                    exception=ActivityTimeout(
                        attr(event, "activity_type", "?"),
                        attr(event, "timeout_kind", "start_to_close"),
                    )
                ),
            )
        elif et is EventType.ACTIVITY_CANCELLED:
            state.index_completion(
                attr(event, "seq"),
                _Completion(exception=ActivityCancelled(attr(event, "activity_type", "?"))),
            )
        elif et is EventType.TIMER_FIRED:
            state.index_completion(attr(event, "seq"), _Completion(value=None))
        elif et is EventType.CHILD_WORKFLOW_COMPLETED:
            state.index_completion(attr(event, "seq"), _Completion(value=attr(event, "result")))
        elif et is EventType.CHILD_WORKFLOW_FAILED:
            cause = _app_error_from(attr(event, "error", {}))
            state.index_completion(
                attr(event, "seq"),
                _Completion(
                    exception=ChildWorkflowFailure(attr(event, "workflow_type", "?"), cause=cause)
                ),
            )
        elif et is EventType.VERSION_MARKER:
            state.index_version(attr(event, "change_id"), attr(event, "version"))
        elif et is EventType.SIDE_EFFECT_RECORDED:
            state.index_side_effect(attr(event, "seq"), attr(event, "value"))
        elif et is EventType.SIGNAL_RECEIVED:
            state.deliver_signal(attr(event, "name"), attr(event, "payload"))
        elif et is EventType.WORKFLOW_CANCEL_REQUESTED:
            state.cancel_requested = True
        if et in _SCHEDULING_EVENTS:
            state.recorded_command_event_count += 1
    state.current_time = current_time
    return state


#: Scheduling events that count toward the versioning "frontier" — those a
#: deterministic body always re-emits as commands on replay (kept in sync with
#: ``executor._RECORDED_COMMAND_EVENTS``).
_SCHEDULING_EVENTS = frozenset(
    {
        EventType.ACTIVITY_SCHEDULED,
        EventType.TIMER_STARTED,
        EventType.TIMER_CANCELLED,
        EventType.CHILD_WORKFLOW_STARTED,
    }
)


@dataclass(slots=True)
class TaskResult:
    """The outcome of running one workflow task (replay + advance)."""

    new_commands: list[Command]
    completed: bool = False
    result: Any = None
    failed: bool = False
    error: ApplicationError | None = None
    continued_as_new: bool = False
    continue_args: list[Any] = field(default_factory=list)
    continue_kwargs: dict[str, Any] = field(default_factory=dict)


def run_workflow_coroutine(coro: Awaitable[Any]) -> tuple[bool, Any]:
    """Step a workflow coroutine until it suspends or finishes.

    Returns ``(finished, result)``. ``finished`` is False if the coroutine parked
    on a :class:`WorkflowSuspended` (awaited an unresolved future). The coroutine
    is driven by ``.send(None)`` because all suspensions surface as a raised
    :class:`WorkflowSuspended`, never as a real ``yield`` of an awaitable — so the
    loop runs to the first park or to ``StopIteration``.
    """
    runner = coro.__await__()
    try:
        runner.send(None)
    except StopIteration as stop:
        return True, stop.value
    except WorkflowSuspended:
        return False, None
    # If the coroutine yielded (awaited a non-engine awaitable), that's a misuse:
    # the workflow awaited something the engine doesn't understand.
    raise NonDeterminismError(
        "workflow awaited a non-engine awaitable; only ctx.* futures may be awaited"
    )


def _app_error_from(data: dict[str, Any]) -> ApplicationError:
    return ApplicationError(
        data.get("message", "activity failed"),
        type=data.get("type"),
        non_retryable=bool(data.get("non_retryable", False)),
        details=data.get("details"),
    )


def _epoch() -> datetime:
    from datetime import UTC

    return datetime(1970, 1, 1, tzinfo=UTC)


__all__ = [
    "ReplayState",
    "TaskResult",
    "build_replay_state",
    "run_workflow_coroutine",
]
