"""A registry + supervisor for the read side's projections.

:class:`ProjectionRegistry` is the composition seam: register every
:class:`Projection`, point it at one :class:`EventStore` /
:class:`ReadModelStore` / :class:`CheckpointStore`, and get back
:class:`ProjectionRuntime`\\ s, a :class:`LagTracker`, an :class:`AsOfProjector`,
and a :class:`BlueGreenRebuilder` — all sharing the same backing stores. This is
what the composition root would build and hand to the API/worker layers.

:class:`ProjectionSupervisor` runs many projections' live tails concurrently as
asyncio tasks and stops them cleanly — the long-lived process the ``api`` (or a
dedicated projection worker) would launch. It is deliberately thin: each
projection gets its own :meth:`ProjectionRuntime.run` task; a fault in one does
not stop the others; :meth:`ProjectionSupervisor.stop` cancels and awaits them.

No infra is imported here; which concrete stores back the registry is the
caller's choice (in-memory for tests, Postgres in production).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable

from app.eventsourcing.projections.bluegreen import (
    BlueGreenRebuilder,
    InMemorySlotDirectory,
    SlotDirectory,
)
from app.eventsourcing.projections.checkpoints import CheckpointStore, ProjectionCheckpoint
from app.eventsourcing.projections.contracts import EventStore
from app.eventsourcing.projections.lag import LagSnapshot, LagTracker
from app.eventsourcing.projections.projection import Projection
from app.eventsourcing.projections.readmodel import ReadModelStore
from app.eventsourcing.projections.runtime import (
    CatchUpResult,
    DeadLetterSink,
    ProjectionRuntime,
    RuntimeConfig,
)
from app.eventsourcing.projections.temporal import AsOfProjector

logger = logging.getLogger(__name__)


class ProjectionRegistry:
    """Holds the wired projections and mints the runtime objects over shared stores."""

    def __init__(
        self,
        *,
        event_store: EventStore,
        read_models: ReadModelStore,
        checkpoints: CheckpointStore,
        slot_directory: SlotDirectory | None = None,
        config: RuntimeConfig | None = None,
        dead_letter: DeadLetterSink | None = None,
    ) -> None:
        self._events = event_store
        self._read_models = read_models
        self._checkpoints = checkpoints
        self._slot_directory = slot_directory or InMemorySlotDirectory()
        self._config = config or RuntimeConfig()
        self._dead_letter = dead_letter
        self._projections: dict[str, Projection] = {}

    def register(self, projection: Projection) -> Projection:
        """Register ``projection`` (its name must be unique). Returns it for chaining."""
        if not projection.name:
            raise ValueError("Projection.name must be set")
        if projection.name in self._projections:
            raise ValueError(f"duplicate projection name {projection.name!r}")
        self._projections[projection.name] = projection
        return projection

    def register_all(self, projections: Iterable[Projection]) -> None:
        for projection in projections:
            self.register(projection)

    def names(self) -> list[str]:
        return sorted(self._projections)

    def get(self, name: str) -> Projection:
        return self._projections[name]

    @property
    def read_models(self) -> ReadModelStore:
        """The shared read-model store (the read facade reads through this)."""
        return self._read_models

    @property
    def checkpoint_store(self) -> CheckpointStore:
        """The shared checkpoint store (positions + applied-event dedupe)."""
        return self._checkpoints

    def runtime(self, name: str, *, namespace: str | None = None) -> ProjectionRuntime:
        """A runtime for one projection (default namespace = the projection's name)."""
        return ProjectionRuntime(
            self._projections[name],
            event_store=self._events,
            read_models=self._read_models,
            checkpoints=self._checkpoints,
            namespace=namespace,
            config=self._config,
            dead_letter=self._dead_letter,
        )

    def lag_tracker(self) -> LagTracker:
        return LagTracker(event_store=self._events, checkpoints=self._checkpoints)

    def as_of(self) -> AsOfProjector:
        return AsOfProjector(event_store=self._events)

    def rebuilder(self) -> BlueGreenRebuilder:
        return BlueGreenRebuilder(
            event_store=self._events,
            read_models=self._read_models,
            checkpoints=self._checkpoints,
            directory=self._slot_directory,
            config=self._config,
        )

    async def catch_up_all(self) -> dict[str, CatchUpResult]:
        """Run a one-shot catch-up for every registered projection (cold start)."""
        results: dict[str, CatchUpResult] = {}
        for name in self.names():
            results[name] = await self.runtime(name).catch_up()
        return results

    async def lag_snapshot(self) -> list[LagSnapshot]:
        """A lag reading for every registered projection (sharing one head read)."""
        return await self.lag_tracker().snapshot_all(self.names())

    async def checkpoints(self) -> list[ProjectionCheckpoint]:
        """The current checkpoint of every registered projection."""
        return [await self._checkpoints.load(name) for name in self.names()]


class ProjectionSupervisor:
    """Runs the live tails of a set of projections as supervised asyncio tasks."""

    def __init__(self, registry: ProjectionRegistry) -> None:
        self._registry = registry
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stop = asyncio.Event()

    async def start(self, names: Iterable[str] | None = None) -> None:
        """Launch a live-tail task per projection (all registered, or a subset)."""
        self._stop.clear()
        targets = list(names) if names is not None else self._registry.names()
        for name in targets:
            if name in self._tasks and not self._tasks[name].done():
                continue
            runtime = self._registry.runtime(name)
            self._tasks[name] = asyncio.create_task(
                self._supervise(name, runtime), name=f"projection:{name}"
            )

    async def _supervise(self, name: str, runtime: ProjectionRuntime) -> None:
        try:
            await runtime.run(stop_event=self._stop)
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            raise
        except Exception:  # noqa: BLE001 - isolate one projection's failure
            logger.exception("projection %s live tail crashed", name)

    async def stop(self) -> None:
        """Signal all tails to stop, cancel, and await their completion."""
        self._stop.set()
        for task in self._tasks.values():
            task.cancel()
        for task in self._tasks.values():
            # A cancelled tail raises CancelledError (a BaseException, so it must
            # be named explicitly); a crashed one already logged in _supervise.
            # Either way, awaiting it here just reaps the task.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    @property
    def running(self) -> list[str]:
        return sorted(n for n, t in self._tasks.items() if not t.done())


def default_projections() -> list[Projection]:
    """The three example projections, instantiated (the demo/default read side)."""
    from app.eventsourcing.projections.examples.canon_audit_view import (
        CanonAuditViewProjection,
    )
    from app.eventsourcing.projections.examples.session_timeline import (
        SessionTimelineProjection,
    )
    from app.eventsourcing.projections.examples.shot_status_board import (
        ShotStatusBoardProjection,
    )

    return [
        SessionTimelineProjection(),
        ShotStatusBoardProjection(),
        CanonAuditViewProjection(),
    ]


__all__ = [
    "ProjectionRegistry",
    "ProjectionSupervisor",
    "default_projections",
]
