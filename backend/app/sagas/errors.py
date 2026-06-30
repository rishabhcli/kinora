"""The saga-engine exception hierarchy.

Two axes matter to the engine's control flow:

* **transient vs permanent** — a transient failure is retried per the step's
  :class:`~app.sagas.policy.RetryPolicy`; a permanent one skips straight to the
  retry-exhausted path (compensation or the configured failure branch). A step
  action signals "don't retry me" by raising :class:`PermanentStepError` (or any
  subclass), and "do retry" by raising anything else (or
  :class:`TransientStepError` explicitly).

* **engine-internal vs step-raised** — :class:`SagaError` subclasses raised *by
  the engine* (unknown step, store conflict, …) are bugs/operational faults and
  propagate; exceptions raised *inside a step action* are caught, classified,
  and recorded as step attempts.

Keeping the taxonomy here (rather than scattered ``raise ValueError``) lets the
engine make a single, testable decision about every failure.
"""

from __future__ import annotations


class SagaError(Exception):
    """Base for every error the saga engine raises or recognises."""


class WorkflowDefinitionError(SagaError):
    """A malformed workflow definition (dup step name, bad branch target, …)."""


class UnknownWorkflowError(SagaError):
    """A run references a workflow name no registry knows about."""


class UnknownStepError(SagaError):
    """A history/branch points at a step name not in the definition."""


class StoreConflictError(SagaError):
    """Optimistic-concurrency clash: the stored revision moved under us.

    Raised by a :class:`~app.sagas.store.DurableStore` when a compare-and-set
    write loses a race — two engines tried to advance the same run. The loser
    re-loads and the recovery sweep / lease mechanism arbitrates ownership.
    """


class RunNotFoundError(SagaError):
    """No persisted run exists for the given id."""


class StepError(SagaError):
    """Base for failures *originating inside a step action*.

    The engine catches these (and any other exception) from an action and turns
    them into a recorded :class:`~app.sagas.history.StepAttempt`. The
    ``transient`` flag drives whether the engine retries.
    """

    #: Default classification; subclasses override.
    transient: bool = True

    def __init__(self, message: str = "", *, transient: bool | None = None) -> None:
        super().__init__(message)
        if transient is not None:
            self.transient = transient


class TransientStepError(StepError):
    """A retryable step failure (provider hiccup, lock contention, …)."""

    transient = True


class PermanentStepError(StepError):
    """A non-retryable step failure — exhaust retries immediately."""

    transient = False


class StepTimeoutError(TransientStepError):
    """A step exceeded its per-attempt timeout (transient by default)."""

    transient = True


class CompensationError(SagaError):
    """A compensation handler itself failed.

    Compensation is best-effort: the engine records the failure and continues
    unwinding the remaining steps rather than aborting the rollback, then
    surfaces the collected failures on the terminal :class:`SagaFailed`.
    """

    def __init__(self, step: str, cause: BaseException) -> None:
        super().__init__(f"compensation for {step!r} failed: {cause!r}")
        self.step = step
        self.cause = cause


class SagaFailed(SagaError):  # noqa: N818 - domain term; the "Error" base is SagaError
    """Terminal: the workflow failed and (if any) compensation has been run.

    Carries the originating cause and the per-step compensation outcomes so the
    caller can surface a precise post-mortem.
    """

    def __init__(
        self,
        run_id: str,
        failed_step: str,
        cause: str,
        *,
        compensated: list[str] | None = None,
        compensation_failures: list[str] | None = None,
    ) -> None:
        super().__init__(
            f"saga {run_id!r} failed at step {failed_step!r}: {cause}"
        )
        self.run_id = run_id
        self.failed_step = failed_step
        self.cause = cause
        self.compensated = list(compensated or [])
        self.compensation_failures = list(compensation_failures or [])


__all__ = [
    "CompensationError",
    "PermanentStepError",
    "RunNotFoundError",
    "SagaError",
    "SagaFailed",
    "StepError",
    "StepTimeoutError",
    "StoreConflictError",
    "TransientStepError",
    "UnknownStepError",
    "UnknownWorkflowError",
    "WorkflowDefinitionError",
]
