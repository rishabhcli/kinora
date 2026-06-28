"""The typed job registry + the ``@job`` decorator.

A :class:`JobDefinition` binds a unique ``name`` to its handler, trigger, retry
policy, and idempotency-key function. Definitions are collected in a
:class:`JobRegistry`; the scheduler walks the registry to find what is due and
the worker looks a run's handler up by name.

Registration is via the :func:`job` decorator::

    @job("budget.reconcile", trigger=every(900), max_attempts=5)
    async def reconcile_budget(ctx: JobContext) -> JobResult:
        ...

The default idempotency key is ``"{name}@{scheduled_for-iso-minute}"`` so two
scheduler nodes that both observe the same due instant create the *same* key and
the store collapses them to one run. A job may override this (e.g. to dedup on a
payload field) by passing ``idempotency_key=lambda name, scheduled_for, payload: ...``.

The decorator can target a module-global default registry (convenient for the
built-in maintenance jobs) or an explicit registry passed via ``registry=``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.jobs.backoff import DEFAULT_POLICY, BackoffPolicy
from app.jobs.triggers import ManualTrigger, Trigger
from app.jobs.types import JobHandler, ScheduledJobState

#: ``(name, scheduled_for, payload) -> idempotency_key``.
IdempotencyKeyFn = Callable[[str, datetime, Mapping[str, Any]], str]


def default_idempotency_key(name: str, scheduled_for: datetime, payload: Mapping[str, Any]) -> str:
    """Collapse the same job at the same due *minute* to one run.

    Truncating to the minute means two scheduler nodes whose clocks differ by a
    few hundred ms still derive the same key for a cron/interval fire.
    """
    minute = scheduled_for.replace(second=0, microsecond=0).isoformat()
    return f"{name}@{minute}"


@dataclass(frozen=True, slots=True)
class JobDefinition:
    """A registered job: its identity, trigger, retry policy, and handler."""

    name: str
    handler: JobHandler
    trigger: Trigger
    backoff: BackoffPolicy = DEFAULT_POLICY
    idempotency_key_fn: IdempotencyKeyFn = default_idempotency_key
    description: str = ""
    singleton: bool = True  # at most one *active* run at a time (dedup on key)
    default_state: ScheduledJobState = ScheduledJobState.ENABLED

    def idempotency_key(
        self, scheduled_for: datetime, payload: Mapping[str, Any] | None = None
    ) -> str:
        """Compute this job's idempotency key for a due instant + optional payload."""
        return self.idempotency_key_fn(self.name, scheduled_for, payload or {})

    @property
    def max_attempts(self) -> int:
        return self.backoff.max_attempts


class JobRegistry:
    """A name-keyed collection of :class:`JobDefinition` (no duplicates)."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobDefinition] = {}

    def register(self, definition: JobDefinition) -> JobDefinition:
        """Register ``definition``; raises on a duplicate name."""
        if definition.name in self._jobs:
            raise ValueError(f"duplicate job registration: {definition.name!r}")
        self._jobs[definition.name] = definition
        return definition

    def get(self, name: str) -> JobDefinition | None:
        """Look up a definition by name (``None`` if unregistered)."""
        return self._jobs.get(name)

    def require(self, name: str) -> JobDefinition:
        """Look up a definition by name; raises :class:`KeyError` if missing."""
        try:
            return self._jobs[name]
        except KeyError as exc:
            raise KeyError(f"no job registered under {name!r}") from exc

    def names(self) -> list[str]:
        """All registered job names (sorted, stable)."""
        return sorted(self._jobs)

    def all(self) -> list[JobDefinition]:
        """All registered definitions (name-sorted)."""
        return [self._jobs[n] for n in self.names()]

    def scheduled(self) -> list[JobDefinition]:
        """Definitions whose trigger can auto-fire (excludes manual-only jobs)."""
        return [d for d in self.all() if not isinstance(d.trigger, ManualTrigger)]

    def __contains__(self, name: object) -> bool:
        return name in self._jobs

    def __len__(self) -> int:
        return len(self._jobs)


#: A module-global default registry the built-in maintenance jobs register into.
DEFAULT_REGISTRY = JobRegistry()


def job(
    name: str,
    *,
    trigger: Trigger | None = None,
    backoff: BackoffPolicy | None = None,
    max_attempts: int | None = None,
    idempotency_key: IdempotencyKeyFn | None = None,
    description: str = "",
    singleton: bool = True,
    default_state: ScheduledJobState = ScheduledJobState.ENABLED,
    registry: JobRegistry | None = None,
) -> Callable[[JobHandler], JobHandler]:
    """Decorator that registers ``func`` as a job and returns it unchanged.

    ``max_attempts`` is a shorthand that overrides the (default) backoff's cap;
    pass an explicit ``backoff`` for full control. Without a ``trigger`` the job
    is manual-only (run on demand). Registers into ``registry`` or, by default,
    the module-global :data:`DEFAULT_REGISTRY`.
    """
    target = registry if registry is not None else DEFAULT_REGISTRY
    policy = backoff or DEFAULT_POLICY
    if max_attempts is not None:
        policy = BackoffPolicy(
            max_attempts=max_attempts,
            base_delay_s=policy.base_delay_s,
            factor=policy.factor,
            max_delay_s=policy.max_delay_s,
            jitter=policy.jitter,
        )

    def decorate(func: JobHandler) -> JobHandler:
        target.register(
            JobDefinition(
                name=name,
                handler=func,
                trigger=trigger or ManualTrigger(),
                backoff=policy,
                idempotency_key_fn=idempotency_key or default_idempotency_key,
                description=description or (func.__doc__ or "").strip().split("\n")[0],
                singleton=singleton,
                default_state=default_state,
            )
        )
        return func

    return decorate


__all__ = [
    "DEFAULT_REGISTRY",
    "IdempotencyKeyFn",
    "JobDefinition",
    "JobRegistry",
    "default_idempotency_key",
    "job",
]
