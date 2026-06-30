"""The context handed to a step's action, compensation, and branch chooser.

A :class:`StepContext` is the step's *only* view of the run. It gives a step:

* the immutable workflow ``input`` and the current step's ``name`` / ``attempt``;
* the **idempotency key** for this step (stable across resume) — the value a
  side effect should dedupe against;
* read access to the durable, JSON-serialisable ``state`` shared across steps,
  plus :meth:`result_of` to read a prior step's recorded result and
  :meth:`set` / :meth:`get` to stash working values that themselves persist;
* the delivered ``signal_payload`` when the step awaited a signal;
* the injected :class:`~app.sagas.clock.Clock` so a step never reads wall time
  directly (keeps tests deterministic).

A step communicates *backwards* (to its own compensation, or to a later step's
branch) only through ``state`` and its returned result — both durable — so the
forward/backward halves of a saga stay coordinated even across a crash.

The context is constructed fresh by the engine per attempt; mutations to
``state`` are flushed to the durable store with the step record.
"""

from __future__ import annotations

from typing import Any

from app.sagas.clock import Clock
from app.sagas.history import RunState


class StepContext:
    """A step's window onto its run (forward action + compensation + branch)."""

    __slots__ = (
        "_run",
        "attempt",
        "clock",
        "idempotency_key",
        "is_compensating",
        "signal_payload",
        "step_name",
    )

    def __init__(
        self,
        run: RunState,
        step_name: str,
        *,
        attempt: int,
        idempotency_key: str,
        clock: Clock,
        signal_payload: Any = None,
        is_compensating: bool = False,
    ) -> None:
        self._run = run
        self.step_name = step_name
        self.attempt = attempt
        self.idempotency_key = idempotency_key
        self.clock = clock
        self.signal_payload = signal_payload
        #: True when this context is driving a compensation (rollback), so an
        #: action body shared between forward/undo can branch on intent.
        self.is_compensating = is_compensating

    # -- run identity ------------------------------------------------------

    @property
    def run_id(self) -> str:
        return self._run.run_id

    @property
    def workflow(self) -> str:
        return self._run.workflow

    @property
    def input(self) -> Any:
        """The immutable workflow input."""
        return self._run.input

    # -- shared durable state ---------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Read a value from the run's durable shared ``context`` dict."""
        return self._run.context.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Write a JSON-serialisable value into the durable shared context.

        Persisted with the step record, so later steps and the recovery path see
        it. Keep values small and serialisable — heavy bytes belong in object
        storage keyed by the idempotency key, not in run state.
        """
        self._run.context[key] = value

    def result_of(self, step_name: str) -> Any:
        """The recorded result of a previously-completed step (or ``None``)."""
        rec = self._run.step_by_name(step_name)
        return rec.result if rec is not None and rec.is_done else None

    def now(self) -> float:
        """Current engine time (use this, never ``time.time()``)."""
        return self.clock.time()


__all__ = ["StepContext"]
