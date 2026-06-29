"""``WorkflowContext`` — the only door from deterministic workflow code to the
outside world.

A workflow body receives a context and reaches everything *durable* through it:

* :meth:`execute_activity` — schedule an activity and await its result (durable,
  retried, timed-out, heartbeated). Returns a :class:`WorkflowFuture` so callers
  can fan out concurrently with :func:`~app.platform.workflows.futures.gather`.
* :meth:`sleep` / :meth:`start_timer` — durable timers in *workflow time* (survive
  crashes; a 30-day sleep costs nothing while parked).
* :meth:`wait_for_signal` / :meth:`signalled` — external **signals** delivered into
  history; :meth:`register_query` — synchronous **queries** over current state.
* :meth:`start_child_workflow` — spawn and await a child execution.
* :meth:`continue_as_new` — close this run and start a fresh one with a compact
  history (the unbounded-loop escape hatch).
* :meth:`patched` / :meth:`get_version` — **versioning** so a code change is safe
  to deploy against in-flight histories.
* :meth:`now` / :meth:`random` / :meth:`uuid4` / :meth:`side_effect` — the
  deterministic substitutes for the wall clock / RNG / UUIDs / arbitrary local
  computation.

The context is *recreated every workflow task*; it holds the per-task command
buffer and a back-reference to the :class:`ReplayState` that owns the
history-resolved futures. Workflow code must never stash the context across an
``await`` boundary and reuse it in a later task (it won't — the engine hands a
fresh one in each replay), and must never call ``asyncio`` / ``time`` / ``random``
directly (see :mod:`app.platform.workflows.sandbox`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.platform.workflows.commands import (
    CancelTimer,
    Command,
    CommandType,
    CompleteWorkflow,
    ContinueAsNew,
    RecordSideEffect,
    RecordVersionMarker,
    ScheduleActivity,
    StartChildWorkflow,
    StartTimer,
)
from app.platform.workflows.determinism import DeterministicRandom, WorkflowTime, uuid_from
from app.platform.workflows.errors import QueryNotRegisteredError
from app.platform.workflows.futures import WorkflowFuture
from app.platform.workflows.retry import RetryPolicy
from app.platform.workflows.versioning import DEFAULT_VERSION, VersioningMixin

if TYPE_CHECKING:
    from app.platform.workflows.replay import ReplayState


class WorkflowInfo:
    """Read-only metadata about the running execution (safe inside workflow code)."""

    __slots__ = ("workflow_id", "run_id", "workflow_type", "task_queue", "attempt")

    def __init__(
        self,
        *,
        workflow_id: str,
        run_id: str,
        workflow_type: str,
        task_queue: str,
        attempt: int,
    ) -> None:
        self.workflow_id = workflow_id
        self.run_id = run_id
        self.workflow_type = workflow_type
        self.task_queue = task_queue
        self.attempt = attempt


class WorkflowContext(VersioningMixin):
    """The durable, deterministic API handed to a workflow body each task."""

    def __init__(self, state: ReplayState, info: WorkflowInfo) -> None:
        self._state = state
        self._info = info
        self._commands: list[Command] = []
        self._random = DeterministicRandom(seed=info.run_id)
        self._time = WorkflowTime(start=state.current_time)
        self._query_handlers: dict[str, Callable[..., Any]] = {}
        self._signal_handlers: dict[str, Callable[[Any], None]] = {}

    # ----- introspection -----------------------------------------------------
    @property
    def info(self) -> WorkflowInfo:
        return self._info

    @property
    def commands(self) -> list[Command]:
        """The commands produced during this task (read by the executor)."""
        return self._commands

    def now(self) -> datetime:
        """Deterministic 'current time' (the timestamp of the event being replayed)."""
        self._time.current = self._state.current_time
        return self._time.now()

    def random(self) -> DeterministicRandom:
        """A run-seeded deterministic RNG (stable across replays)."""
        return self._random

    def uuid4(self) -> str:
        """A deterministic UUID derived from the run id + a per-call sequence."""
        seq = self._state.next_seq()
        return uuid_from(self._info.run_id, seq)

    # ----- activities --------------------------------------------------------
    def execute_activity(
        self,
        activity_type: str,
        *args: Any,
        task_queue: str | None = None,
        retry_policy: RetryPolicy | None = None,
        start_to_close_timeout_s: float | None = None,
        schedule_to_close_timeout_s: float | None = None,
        heartbeat_timeout_s: float | None = None,
        **kwargs: Any,
    ) -> WorkflowFuture[Any]:
        """Schedule an activity and return a future for its result.

        ``await``\\ing the future yields the activity's result, or raises
        :class:`~app.platform.workflows.errors.ActivityFailure` /
        :class:`ActivityTimeout` after retries/timeouts are exhausted — both
        catchable so the workflow can compensate.
        """
        seq = self._state.next_seq()
        cmd = ScheduleActivity(
            type=CommandType.SCHEDULE_ACTIVITY,
            seq=seq,
            activity_type=activity_type,
            args=list(args),
            kwargs=dict(kwargs),
            task_queue=task_queue,
            retry_policy=retry_policy,
            start_to_close_timeout_s=start_to_close_timeout_s,
            schedule_to_close_timeout_s=schedule_to_close_timeout_s,
            heartbeat_timeout_s=heartbeat_timeout_s,
        )
        self._commands.append(cmd)
        return self._state.future_for(seq)

    # ----- timers ------------------------------------------------------------
    def start_timer(self, delay_s: float) -> WorkflowFuture[None]:
        """Arm a durable timer; the future resolves when it fires."""
        seq = self._state.next_seq()
        self._commands.append(
            StartTimer(type=CommandType.START_TIMER, seq=seq, delay_s=max(0.0, delay_s))
        )
        return self._state.future_for(seq)

    async def sleep(self, delay_s: float) -> None:
        """Durable sleep in workflow time (parks the workflow, costs nothing)."""
        await self.start_timer(delay_s)

    def cancel_timer(self, timer_future: WorkflowFuture[None]) -> None:
        """Cancel a pending timer by the future :meth:`start_timer` returned."""
        seq = self._state.next_seq()
        self._commands.append(
            CancelTimer(type=CommandType.CANCEL_TIMER, seq=seq, timer_seq=timer_future.seq)
        )

    # ----- signals / queries -------------------------------------------------
    def register_signal(self, name: str, handler: Callable[[Any], None]) -> None:
        """Register a handler invoked (during replay) for each matching signal."""
        self._signal_handlers[name] = handler

    def register_query(self, name: str, handler: Callable[..., Any]) -> None:
        """Register a synchronous query handler over current workflow state."""
        self._query_handlers[name] = handler

    def signalled(self, name: str) -> list[Any]:
        """All payloads received so far for signal ``name`` (in arrival order)."""
        return self._state.signals_for(name)

    def wait_for_signal(self, name: str) -> WorkflowFuture[Any]:
        """A future that resolves with the next *unconsumed* payload of ``name``."""
        return self._state.signal_future(name)

    def run_query(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke a registered query handler (used by the client/query path)."""
        try:
            handler = self._query_handlers[name]
        except KeyError as exc:
            raise QueryNotRegisteredError(name) from exc
        return handler(*args, **kwargs)

    @property
    def query_names(self) -> list[str]:
        return sorted(self._query_handlers)

    @property
    def is_cancelled(self) -> bool:
        """True once a cancel request appears in history."""
        return self._state.cancel_requested

    # ----- child workflows ---------------------------------------------------
    def start_child_workflow(
        self,
        workflow_type: str,
        *args: Any,
        child_workflow_id: str | None = None,
        task_queue: str | None = None,
        **kwargs: Any,
    ) -> WorkflowFuture[Any]:
        """Start a child workflow and return a future for its result."""
        seq = self._state.next_seq()
        child_id = child_workflow_id or f"{self._info.workflow_id}:child:{seq}"
        self._commands.append(
            StartChildWorkflow(
                type=CommandType.START_CHILD_WORKFLOW,
                seq=seq,
                workflow_type=workflow_type,
                child_workflow_id=child_id,
                args=list(args),
                kwargs=dict(kwargs),
                task_queue=task_queue,
            )
        )
        return self._state.future_for(seq)

    # ----- continue-as-new ---------------------------------------------------
    def continue_as_new(self, *args: Any, **kwargs: Any) -> None:
        """Request continue-as-new; raises to unwind the workflow body.

        The executor turns the emitted command into a fresh run with a compact
        history. Raising :class:`_ContinueAsNew` unwinds the body so no code runs
        past this point in the current run.
        """
        seq = self._state.next_seq()
        self._commands.append(
            ContinueAsNew(
                type=CommandType.CONTINUE_AS_NEW, seq=seq, args=list(args), kwargs=dict(kwargs)
            )
        )
        raise _ContinueAsNewSignal()

    # ----- side effects ------------------------------------------------------
    async def side_effect(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` once and memoise its result into history.

        For genuinely non-deterministic *local* computation that you don't want
        to pay for as a full activity (a random pick, a local timestamp). On the
        first execution ``fn`` runs and the value is recorded; on replay the value
        is read back, never recomputed — keeping determinism intact.
        """
        seq = self._state.next_seq()
        recorded = self._state.recorded_side_effect(seq)
        if not self._state.is_no_side_effect(recorded):
            return recorded
        value = fn()
        self._commands.append(
            RecordSideEffect(type=CommandType.RECORD_SIDE_EFFECT, seq=seq, value=value)
        )
        return value

    # ----- versioning (implemented in VersioningMixin via these hooks) -------
    def _emit_version_marker(self, change_id: str, version: int) -> None:
        # Version markers are identified by ``change_id`` (looked up by name in
        # history), so they deliberately do NOT draw from the main command-seq
        # counter — a ``get_version`` call must not shift the seq of the
        # activities/timers that follow it, whether it *records* (fresh run) or
        # *reads back* (replay). Using seq=-1 keeps the seq stream identical on
        # both paths, which is what keeps replay deterministic.
        self._commands.append(
            RecordVersionMarker(
                type=CommandType.RECORD_VERSION_MARKER,
                seq=-1,
                change_id=change_id,
                version=version,
            )
        )

    def _recorded_version(self, change_id: str) -> int | None:
        return self._state.recorded_version(change_id)

    def _reached_change_frontier(self) -> bool:
        # At the frontier once the body has emitted as many *scheduling* commands
        # as the history has recorded command-events: beyond that point there is
        # no recorded history to replay, so this is fresh code execution. Before
        # it, we're replaying an older run that never recorded a marker here.
        emitted = sum(1 for c in self._commands if c.seq >= 0)
        return emitted >= self._state.recorded_command_event_count

    # ----- completion (used by the executor, not workflow code) --------------
    def _complete(self, result: Any) -> None:
        seq = self._state.next_seq()
        self._commands.append(
            CompleteWorkflow(type=CommandType.COMPLETE_WORKFLOW, seq=seq, result=result)
        )


class _ContinueAsNewSignal(BaseException):
    """Internal: unwinds the workflow body when continue-as-new is requested."""


__all__ = ["DEFAULT_VERSION", "WorkflowContext", "WorkflowInfo"]
