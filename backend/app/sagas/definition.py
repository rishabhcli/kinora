"""The workflow definition DSL — declare ordered/branching, compensable steps.

A :class:`Workflow` is an *ordered list* of named :class:`Step` s. Each step
declares:

* an **action** — ``async (ctx) -> result`` (the forward side effect);
* an optional **compensation** — ``async (ctx) -> None`` that *undoes* the
  action's side effect (run in reverse on a saga failure past this step);
* a :class:`~app.sagas.policy.RetryPolicy` and
  :class:`~app.sagas.policy.TimeoutPolicy`;
* optional **branching** — ``branch(ctx) -> next-step-name | None`` chooses the
  next step to run (``None`` = fall through to the next step in order); a target
  of :data:`END` finishes the run successfully;
* an optional **await_signal** name — the engine parks the run until that signal
  arrives (with an optional timeout that routes to a branch).

The DSL is *pure data*: actions/compensations are injected callables, so tests
drive a workflow with plain async functions and never touch a provider, DB, or
ffmpeg. :meth:`Workflow.validate` rejects malformed graphs at registration time
(duplicate names, branch targets that don't exist) so a bad definition fails
fast rather than mid-run.

The forward order is the list order; branching only *jumps* within that order
(forward or to ``END``) — the engine never executes a step twice, so a branch
target must be at or after the current step. This keeps replay linear and the
compensation stack well-defined (reverse of the steps that actually ran).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.sagas.errors import WorkflowDefinitionError
from app.sagas.policy import DEFAULT_RETRY, NO_TIMEOUT, RetryPolicy, TimeoutPolicy

if TYPE_CHECKING:
    from app.sagas.context import StepContext

#: Sentinel branch target meaning "finish the run successfully now".
END = "__end__"

#: A forward action: receives the step context, returns a JSON-serialisable
#: result that is durably recorded and replayed on resume.
Action = Callable[["StepContext"], Awaitable[Any]]
#: A compensation: undoes the action's side effect; result is ignored.
Compensation = Callable[["StepContext"], Awaitable[None]]
#: A branch chooser: returns the next step name, ``END``, or ``None`` (fall
#: through). Pure and synchronous — it reads ``ctx`` only, no side effects.
Branch = Callable[["StepContext"], str | None]


@dataclass(frozen=True, slots=True)
class Step:
    """One node of a workflow.

    Attributes:
        name: unique within the workflow; the step's stable identity in history.
        action: the forward side effect.
        compensation: undoes ``action`` (None = nothing to undo).
        retry: retry policy for the action.
        timeout: per-attempt / total deadlines for the action.
        branch: chooses the next step (None = fall through to the next in order).
        await_signal: park until this named signal arrives before running the
            action (the delivered payload is exposed on the context).
        await_timeout_s: if waiting on a signal, give up after this long.
        on_await_timeout: branch target when ``await_timeout_s`` elapses
            (defaults to failing the step).
        idempotency_salt: bump to force a logically-changed step to re-run rather
            than dedupe against a prior attempt's idempotency key.
    """

    name: str
    action: Action
    compensation: Compensation | None = None
    retry: RetryPolicy = DEFAULT_RETRY
    timeout: TimeoutPolicy = NO_TIMEOUT
    branch: Branch | None = None
    await_signal: str | None = None
    await_timeout_s: float | None = None
    on_await_timeout: str | None = None
    idempotency_salt: str = ""

    @property
    def compensable(self) -> bool:
        return self.compensation is not None


@dataclass(frozen=True, slots=True)
class Workflow:
    """An ordered, validated list of compensable steps."""

    name: str
    steps: tuple[Step, ...]
    #: Documentation only; the engine ignores it.
    description: str = ""

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Reject a malformed graph at construction time."""
        if not self.steps:
            raise WorkflowDefinitionError(f"workflow {self.name!r} has no steps")
        names = [s.name for s in self.steps]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise WorkflowDefinitionError(
                f"workflow {self.name!r} has duplicate step names: {sorted(dupes)}"
            )
        valid_targets = set(names) | {END}
        index = {n: i for i, n in enumerate(names)}
        for i, step in enumerate(self.steps):
            for target_name, kind in (
                (step.on_await_timeout, "on_await_timeout"),
            ):
                if target_name is not None and target_name not in valid_targets:
                    raise WorkflowDefinitionError(
                        f"step {step.name!r} {kind} target {target_name!r} "
                        f"is not a step in {self.name!r}"
                    )
                if (
                    target_name is not None
                    and target_name != END
                    and index[target_name] < i
                ):
                    raise WorkflowDefinitionError(
                        f"step {step.name!r} {kind} target {target_name!r} points "
                        "backwards; branches must jump forward or to END"
                    )
            if step.await_timeout_s is not None and step.await_signal is None:
                raise WorkflowDefinitionError(
                    f"step {step.name!r} sets await_timeout_s without await_signal"
                )

    # -- lookups -----------------------------------------------------------

    def index_of(self, name: str) -> int:
        for i, step in enumerate(self.steps):
            if step.name == name:
                return i
        raise WorkflowDefinitionError(f"no step {name!r} in workflow {self.name!r}")

    def step_at(self, index: int) -> Step | None:
        if 0 <= index < len(self.steps):
            return self.steps[index]
        return None

    def step_by_name(self, name: str) -> Step:
        return self.steps[self.index_of(name)]


@dataclass
class WorkflowBuilder:
    """A small fluent helper to assemble a :class:`Workflow`.

    Optional sugar over the frozen dataclasses for readability in the example
    workflows; everything it builds is equivalent to constructing ``Step`` /
    ``Workflow`` directly.
    """

    name: str
    description: str = ""
    _steps: list[Step] = field(default_factory=list)

    def step(
        self,
        name: str,
        action: Action,
        *,
        compensation: Compensation | None = None,
        retry: RetryPolicy = DEFAULT_RETRY,
        timeout: TimeoutPolicy = NO_TIMEOUT,
        branch: Branch | None = None,
        await_signal: str | None = None,
        await_timeout_s: float | None = None,
        on_await_timeout: str | None = None,
        idempotency_salt: str = "",
    ) -> WorkflowBuilder:
        self._steps.append(
            Step(
                name=name,
                action=action,
                compensation=compensation,
                retry=retry,
                timeout=timeout,
                branch=branch,
                await_signal=await_signal,
                await_timeout_s=await_timeout_s,
                on_await_timeout=on_await_timeout,
                idempotency_salt=idempotency_salt,
            )
        )
        return self

    def build(self) -> Workflow:
        return Workflow(name=self.name, steps=tuple(self._steps), description=self.description)


__all__ = [
    "END",
    "Action",
    "Branch",
    "Compensation",
    "Step",
    "Workflow",
    "WorkflowBuilder",
]
