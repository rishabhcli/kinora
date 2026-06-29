"""Workflow & activity registries + the ``@workflow`` / ``@activity`` decorators.

A workflow is registered by name and resolved to its callable at execution time;
the **name** (not the function object) is what's stored in history, so a workflow
can be re-deployed as long as the registered name and its replay behaviour stay
compatible (that's what versioning is for).

* :func:`activity` marks an ``async`` (or sync) callable as an activity and
  records its default options (retry policy, timeouts). Activities run *outside*
  the deterministic sandbox — they're where real I/O (DashScope calls, DB writes,
  ffmpeg) lives — so they may be non-deterministic and are made durable by
  at-least-once + retries instead.
* :func:`workflow` marks an ``async`` callable as a workflow definition. Its body
  must be deterministic and only reach the outside world through the injected
  :class:`~app.platform.workflows.context.WorkflowContext`.

Both decorators attach metadata and register into module-level default
registries; tests build isolated registries to avoid cross-test leakage.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.platform.workflows.errors import WorkflowDefinitionError
from app.platform.workflows.retry import DEFAULT_RETRY_POLICY, RetryPolicy


@dataclass(slots=True)
class ActivityDefinition:
    """A registered activity: its name, callable, and default execution options."""

    name: str
    fn: Callable[..., Any]
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY
    start_to_close_timeout_s: float | None = None
    schedule_to_close_timeout_s: float | None = None
    heartbeat_timeout_s: float | None = None
    is_async: bool = True

    def __post_init__(self) -> None:
        self.is_async = inspect.iscoroutinefunction(self.fn)


@dataclass(slots=True)
class WorkflowDefinition:
    """A registered workflow: its name, callable, and declared signal/query names.

    ``signal_names`` / ``query_names`` are populated when the workflow registers
    handlers at runtime via the context; they default empty here and exist mainly
    for introspection/admin surfaces.
    """

    name: str
    fn: Callable[..., Any]
    default_task_queue: str = "default"
    signal_names: set[str] = field(default_factory=set)
    query_names: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not inspect.iscoroutinefunction(self.fn):
            raise WorkflowDefinitionError(
                f"workflow {self.name!r} must be an async def (got a sync callable)"
            )


class ActivityRegistry:
    """Name → :class:`ActivityDefinition` lookup."""

    def __init__(self) -> None:
        self._defs: dict[str, ActivityDefinition] = {}

    def register(self, definition: ActivityDefinition) -> None:
        if definition.name in self._defs:
            raise WorkflowDefinitionError(f"activity already registered: {definition.name!r}")
        self._defs[definition.name] = definition

    def get(self, name: str) -> ActivityDefinition:
        try:
            return self._defs[name]
        except KeyError as exc:
            raise WorkflowDefinitionError(f"no activity registered: {name!r}") from exc

    def __contains__(self, name: str) -> bool:
        return name in self._defs

    def names(self) -> list[str]:
        return sorted(self._defs)


class WorkflowRegistry:
    """Name → :class:`WorkflowDefinition` lookup."""

    def __init__(self) -> None:
        self._defs: dict[str, WorkflowDefinition] = {}

    def register(self, definition: WorkflowDefinition) -> None:
        if definition.name in self._defs:
            raise WorkflowDefinitionError(f"workflow already registered: {definition.name!r}")
        self._defs[definition.name] = definition

    def get(self, name: str) -> WorkflowDefinition:
        try:
            return self._defs[name]
        except KeyError as exc:
            raise WorkflowDefinitionError(f"no workflow registered: {name!r}") from exc

    def __contains__(self, name: str) -> bool:
        return name in self._defs

    def names(self) -> list[str]:
        return sorted(self._defs)


#: Process-wide default registries (decorators populate these unless told otherwise).
DEFAULT_ACTIVITY_REGISTRY = ActivityRegistry()
DEFAULT_WORKFLOW_REGISTRY = WorkflowRegistry()


def activity(
    *,
    name: str | None = None,
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    start_to_close_timeout_s: float | None = None,
    schedule_to_close_timeout_s: float | None = None,
    heartbeat_timeout_s: float | None = None,
    registry: ActivityRegistry | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: register ``fn`` as an activity with default options."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        definition = ActivityDefinition(
            name=name or fn.__name__,
            fn=fn,
            retry_policy=retry_policy,
            start_to_close_timeout_s=start_to_close_timeout_s,
            schedule_to_close_timeout_s=schedule_to_close_timeout_s,
            heartbeat_timeout_s=heartbeat_timeout_s,
        )
        (registry or DEFAULT_ACTIVITY_REGISTRY).register(definition)
        fn.__kinora_activity__ = definition  # type: ignore[attr-defined]
        return fn

    return decorate


def workflow(
    *,
    name: str | None = None,
    task_queue: str = "default",
    registry: WorkflowRegistry | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: register ``fn`` as a workflow definition."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        definition = WorkflowDefinition(
            name=name or fn.__name__,
            fn=fn,
            default_task_queue=task_queue,
        )
        (registry or DEFAULT_WORKFLOW_REGISTRY).register(definition)
        fn.__kinora_workflow__ = definition  # type: ignore[attr-defined]
        return fn

    return decorate


__all__ = [
    "DEFAULT_ACTIVITY_REGISTRY",
    "DEFAULT_WORKFLOW_REGISTRY",
    "ActivityDefinition",
    "ActivityRegistry",
    "WorkflowDefinition",
    "WorkflowRegistry",
    "activity",
    "workflow",
]
