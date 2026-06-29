"""Exception hierarchy for the durable-execution engine.

The engine distinguishes three broad failure classes, because each drives a
different recovery path in the executor and the worker runtime:

* :class:`WorkflowError` — the root; everything below it.
* :class:`NonDeterminismError` — replaying a workflow's code against its recorded
  history diverged (a command the code now wants doesn't match the next recorded
  event). This is *fatal* to the run: the deployed code is incompatible with the
  history and the workflow task is failed (not retried into a loop) so an operator
  notices. Versioning/patching (:mod:`app.platform.workflows.versioning`) exists
  precisely to avoid hitting this on a deploy.
* :class:`ActivityFailure` / :class:`ActivityTimeout` / :class:`ActivityCancelled`
  — an activity execution failed, timed out, or was cancelled. These are surfaced
  *inside* the workflow as catchable exceptions, so workflow code can compensate.

The retryable/non-retryable distinction (:class:`ApplicationError`) mirrors
Temporal: an application error can be flagged non-retryable to short-circuit the
activity retry policy regardless of the remaining attempt budget.
"""

from __future__ import annotations

from typing import Any


class WorkflowError(Exception):
    """Root of the durable-execution engine's exception hierarchy."""


class WorkflowDefinitionError(WorkflowError):
    """A workflow/activity was declared incorrectly (caught at registration)."""


class WorkflowAlreadyExistsError(WorkflowError):
    """Tried to start a workflow whose id already has an open execution."""

    def __init__(self, workflow_id: str) -> None:
        super().__init__(f"workflow execution already running: {workflow_id!r}")
        self.workflow_id = workflow_id


class WorkflowNotFoundError(WorkflowError):
    """Referenced a workflow id that has no execution in the store."""

    def __init__(self, workflow_id: str) -> None:
        super().__init__(f"no workflow execution: {workflow_id!r}")
        self.workflow_id = workflow_id


class QueryNotRegisteredError(WorkflowError):
    """A query was issued whose name the workflow never registered."""

    def __init__(self, name: str) -> None:
        super().__init__(f"workflow has no query handler named {name!r}")
        self.name = name


class NonDeterminismError(WorkflowError):
    """Replay diverged from recorded history (fatal to the workflow task).

    Carries the expected event (from history) and the command the freshly-run
    workflow code produced, so the failure message points straight at the
    incompatible step.
    """

    def __init__(self, message: str, *, expected: Any = None, actual: Any = None) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual


class WorkflowSuspended(WorkflowError):  # noqa: N818 - control-flow signal, not an "Error"
    """Internal control-flow signal: the workflow blocked awaiting new events.

    Raised by the determinism layer when workflow code awaits a future that has
    no resolution yet in history. The executor catches it to end the current
    workflow task (the workflow is parked until the awaited event arrives). Never
    escapes the engine to user code.
    """


class WorkflowCancelled(WorkflowError):  # noqa: N818 - surfaced inside workflow code
    """Raised inside workflow code when the workflow has been cancelled.

    Workflow code may catch this to run compensation before returning, mirroring
    Temporal's cancellation-as-exception model.
    """


class ApplicationError(WorkflowError):
    """A domain failure raised by activity (or workflow) code.

    ``non_retryable`` short-circuits the activity retry policy. ``type`` is a
    stable string tag preserved across the serialize/deserialize round-trip so a
    workflow can branch on the *kind* of failure after a crash+replay.
    """

    def __init__(
        self,
        message: str,
        *,
        type: str | None = None,  # noqa: A002 - intentional public field name
        non_retryable: bool = False,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.type = type or self.__class__.__name__
        self.non_retryable = non_retryable
        self.details = details


class ActivityFailure(WorkflowError):  # noqa: N818 - Temporal-style catchable name
    """An activity exhausted its retries (or failed non-retryably).

    Surfaced inside the workflow as a catchable exception. ``cause`` preserves the
    last underlying :class:`ApplicationError` (type/message/details) so the
    workflow can compensate based on what actually went wrong.
    """

    def __init__(self, activity_type: str, *, cause: ApplicationError) -> None:
        super().__init__(f"activity {activity_type!r} failed: {cause.message}")
        self.activity_type = activity_type
        self.cause = cause


class ActivityTimeout(WorkflowError):  # noqa: N818 - Temporal-style catchable name
    """An activity blew a start-to-close / schedule-to-close / heartbeat timeout."""

    def __init__(self, activity_type: str, kind: str) -> None:
        super().__init__(f"activity {activity_type!r} timed out ({kind})")
        self.activity_type = activity_type
        self.kind = kind


class ActivityCancelled(WorkflowError):  # noqa: N818 - Temporal-style catchable name
    """An in-flight activity was cancelled (cooperatively, at a heartbeat)."""

    def __init__(self, activity_type: str) -> None:
        super().__init__(f"activity {activity_type!r} cancelled")
        self.activity_type = activity_type


class ChildWorkflowFailure(WorkflowError):  # noqa: N818 - Temporal-style catchable name
    """A child workflow failed; surfaced inside the parent as catchable."""

    def __init__(self, workflow_type: str, *, cause: ApplicationError) -> None:
        super().__init__(f"child workflow {workflow_type!r} failed: {cause.message}")
        self.workflow_type = workflow_type
        self.cause = cause


__all__ = [
    "ActivityCancelled",
    "ActivityFailure",
    "ActivityTimeout",
    "ApplicationError",
    "ChildWorkflowFailure",
    "NonDeterminismError",
    "QueryNotRegisteredError",
    "WorkflowAlreadyExistsError",
    "WorkflowCancelled",
    "WorkflowDefinitionError",
    "WorkflowError",
    "WorkflowNotFoundError",
    "WorkflowSuspended",
]
