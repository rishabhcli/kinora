"""Saga definitions + the registry — how a saga is declared and looked up.

An *orchestration* saga is declared as an ordered list of :class:`SagaStep`\\ s,
each pairing a forward ``action`` with an optional ``compensation`` (and its own
retry policies + timeout). A :class:`SagaDefinition` binds those steps to a
stable ``name`` and an optional overall ``deadline_s``. The engine drives the
definition; the definition holds no mutable state, so one definition is reused
across all its running instances.

The :class:`SagaRegistry` maps names → definitions, exactly like
:class:`app.jobs.registry` maps names → handlers. The orchestrator resumes a
crashed saga by loading its instance, looking the definition up by name, and
re-driving from the durable cursor — so the registry must be populated before a
recovering worker starts (the engine raises a clear error if a stored instance
references an unknown definition, which prevents silently dropping a saga whose
code was removed).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.distributed.sagas.backoff import (
    DEFAULT_COMPENSATION_POLICY,
    DEFAULT_FORWARD_POLICY,
    BackoffPolicy,
)
from app.distributed.sagas.types import StepHandler


@dataclass(frozen=True, slots=True)
class SagaStep:
    """One step of an orchestration saga: a forward action + its compensation.

    ``action`` is required; ``compensation`` is optional (a read-only or naturally
    idempotent step needs no undo — its compensation is a no-op). ``retry`` /
    ``compensation_retry`` default to the package policies but can be tuned per
    step. ``timeout_s`` bounds a single forward (or compensation) invocation; the
    engine treats a timeout as a retryable failure.
    """

    name: str
    action: StepHandler
    compensation: StepHandler | None = None
    retry: BackoffPolicy = DEFAULT_FORWARD_POLICY
    compensation_retry: BackoffPolicy = DEFAULT_COMPENSATION_POLICY
    timeout_s: float | None = None

    @property
    def has_compensation(self) -> bool:
        return self.compensation is not None


@dataclass(frozen=True, slots=True)
class SagaDefinition:
    """A named, ordered sequence of compensatable steps + an optional saga deadline.

    ``deadline_s`` is the wall-clock budget for the whole saga from start; when it
    elapses the engine transitions the instance to ``TIMED_OUT`` and compensates.
    A definition is immutable and shared across instances.
    """

    name: str
    steps: tuple[SagaStep, ...]
    deadline_s: float | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError(f"saga definition {self.name!r} has no steps")
        names = [s.name for s in self.steps]
        if len(names) != len(set(names)):
            raise ValueError(f"saga definition {self.name!r} has duplicate step names: {names}")

    def step_at(self, index: int) -> SagaStep:
        return self.steps[index]

    def index_of(self, step_name: str) -> int:
        for i, s in enumerate(self.steps):
            if s.name == step_name:
                return i
        raise KeyError(step_name)

    @property
    def step_count(self) -> int:
        return len(self.steps)


def saga(
    name: str,
    *steps: SagaStep,
    deadline_s: float | None = None,
    description: str = "",
) -> SagaDefinition:
    """Convenience constructor: ``saga("name", step1, step2, ...)``."""
    return SagaDefinition(
        name=name, steps=tuple(steps), deadline_s=deadline_s, description=description
    )


def step(
    name: str,
    action: StepHandler,
    *,
    compensation: StepHandler | None = None,
    retry: BackoffPolicy = DEFAULT_FORWARD_POLICY,
    compensation_retry: BackoffPolicy = DEFAULT_COMPENSATION_POLICY,
    timeout_s: float | None = None,
) -> SagaStep:
    """Convenience constructor for a :class:`SagaStep`."""
    return SagaStep(
        name=name,
        action=action,
        compensation=compensation,
        retry=retry,
        compensation_retry=compensation_retry,
        timeout_s=timeout_s,
    )


class UnknownSagaError(KeyError):
    """Raised when resolving a saga definition name that was never registered."""


@dataclass(slots=True)
class SagaRegistry:
    """A name → :class:`SagaDefinition` map (mirrors :mod:`app.jobs.registry`)."""

    _defs: dict[str, SagaDefinition] = field(default_factory=dict)

    def register(self, definition: SagaDefinition) -> SagaDefinition:
        """Register ``definition`` (raises on a duplicate name)."""
        if definition.name in self._defs:
            raise ValueError(f"saga definition {definition.name!r} already registered")
        self._defs[definition.name] = definition
        return definition

    def get(self, name: str) -> SagaDefinition:
        """Look up a definition by name (raises :class:`UnknownSagaError` if absent)."""
        try:
            return self._defs[name]
        except KeyError:
            raise UnknownSagaError(name) from None

    def has(self, name: str) -> bool:
        return name in self._defs

    def names(self) -> list[str]:
        return sorted(self._defs)


__all__ = [
    "SagaDefinition",
    "SagaRegistry",
    "SagaStep",
    "UnknownSagaError",
    "saga",
    "step",
]
