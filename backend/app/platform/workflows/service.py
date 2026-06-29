"""``WorkflowService`` — the one-call facade that wires the engine together.

Bundles a :class:`WorkflowStore`, the workflow/activity registries, a
:class:`WorkflowClient`, and a :class:`Worker` behind a single object the rest of
the backend (composition root, an API route, a ``python -m`` entrypoint) can hold.
It mirrors :class:`app.jobs.service.JobService` in spirit: build it, then either
drive the pieces directly or call :meth:`run` to spin the worker loops.

:func:`build_workflow_service` is the convenience constructor:

* with no ``store`` it uses the zero-infra :class:`InMemoryWorkflowStore`
  (development, tests, the harness);
* given a session factory it builds the
  :class:`~app.platform.workflows.db_store.PostgresWorkflowStore`.

The service is deliberately *not* wired into the composition root here (the
engine is exercised through this facade / the harness), keeping the package
additive and collision-free with the other parallel platform packages. Wiring a
``workflow-worker`` compose service that runs ``python -m app.platform.workflows``
is a documented future step (see ``DESIGN.md``).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.jobs.clock import Clock, SystemClock
from app.platform.workflows.client import WorkflowClient, WorkflowHandle
from app.platform.workflows.memory_store import InMemoryWorkflowStore
from app.platform.workflows.registry import (
    DEFAULT_ACTIVITY_REGISTRY,
    DEFAULT_WORKFLOW_REGISTRY,
    ActivityRegistry,
    WorkflowRegistry,
)
from app.platform.workflows.store import WorkflowExecution, WorkflowStore
from app.platform.workflows.worker import Worker, WorkerConfig


class WorkflowService:
    """Facade over store + registries + client + worker."""

    def __init__(
        self,
        store: WorkflowStore,
        *,
        workflows: WorkflowRegistry | None = None,
        activities: ActivityRegistry | None = None,
        task_queues: Iterable[str] = ("default",),
        clock: Clock | None = None,
        config: WorkerConfig | None = None,
    ) -> None:
        self.store = store
        self.workflows = workflows or DEFAULT_WORKFLOW_REGISTRY
        self.activities = activities or DEFAULT_ACTIVITY_REGISTRY
        self._clock = clock or SystemClock()
        self.client = WorkflowClient(self.store, self.workflows, clock=self._clock)
        self.worker = Worker(
            self.store,
            self.workflows,
            self.activities,
            task_queues=task_queues,
            clock=self._clock,
            config=config or WorkerConfig(),
        )

    async def start(
        self, workflow_type: str, *args: Any, workflow_id: str, **kwargs: Any
    ) -> WorkflowHandle:
        return await self.client.start_workflow(
            workflow_type, *args, workflow_id=workflow_id, **kwargs
        )

    async def signal(self, workflow_id: str, name: str, payload: Any = None) -> None:
        await self.client.signal_workflow(workflow_id, name, payload)

    async def query(self, workflow_id: str, name: str, *args: Any, **kwargs: Any) -> Any:
        return await self.client.query_workflow(workflow_id, name, *args, **kwargs)

    async def cancel(self, workflow_id: str) -> None:
        await self.client.cancel_workflow(workflow_id)

    async def describe(self, workflow_id: str) -> WorkflowExecution | None:
        return await self.client.describe(workflow_id)

    async def get_result(self, workflow_id: str) -> Any:
        return await self.client.get_result(workflow_id)

    async def run(self) -> None:  # pragma: no cover - real-time loop
        """Spin the worker poll loops until cancelled."""
        await self.worker.run()

    def stop(self) -> None:  # pragma: no cover
        self.worker.stop()


def build_workflow_service(
    *,
    store: WorkflowStore | None = None,
    workflows: WorkflowRegistry | None = None,
    activities: ActivityRegistry | None = None,
    task_queues: Iterable[str] = ("default",),
    clock: Clock | None = None,
    config: WorkerConfig | None = None,
) -> WorkflowService:
    """Construct a :class:`WorkflowService` (in-memory store by default)."""
    return WorkflowService(
        store or InMemoryWorkflowStore(),
        workflows=workflows,
        activities=activities,
        task_queues=task_queues,
        clock=clock,
        config=config,
    )


__all__ = ["WorkflowService", "build_workflow_service"]
