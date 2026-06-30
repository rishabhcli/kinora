"""A registry + admin surface for the read side's projections.

:class:`ProjectionRegistry` is the composition seam: register every
:class:`~app.datalayer.projector.Projection`, point them at one shared
``EventStore`` / ``ReadModelStore`` / ``CheckpointStore``, and get back
:class:`~app.datalayer.projector.ProjectionRunner`\\ s over those stores. This is
what a composition root builds and hands to the API / a projection worker.

It also exposes the **admin operations** an operator drives:

* :meth:`catch_up_all` — a one-shot catch-up for every projection (cold start);
* :meth:`rebuild_projection` — reset + replay one named projection from zero
  (the ``rebuild_projection(name)`` admin op);
* :meth:`rebuild_all` — rebuild every registered projection;
* :meth:`checkpoints` / :meth:`lag` — operational visibility.

No infrastructure is imported here; which concrete stores back the registry is
the caller's choice (in-memory for tests, Postgres in production).
"""

from __future__ import annotations

from collections.abc import Iterable

from app.core.logging import get_logger
from app.datalayer.checkpoints import (
    CheckpointStore,
    InMemoryCheckpointStore,
    ProjectionCheckpoint,
)
from app.datalayer.projector import (
    CatchUpResult,
    Projection,
    ProjectionRunner,
    RunnerConfig,
)
from app.datalayer.readmodel import InMemoryReadModelStore, ReadModelStore
from app.eventsourcing.store.contracts import EventStore

logger = get_logger("app.datalayer.registry")


class UnknownProjectionError(KeyError):
    """Raised when an admin operation names a projection that isn't registered."""

    def __init__(self, name: str) -> None:
        self.projection = name
        super().__init__(name)


class ProjectionRegistry:
    """Holds the wired projections and mints runners over shared stores."""

    def __init__(
        self,
        *,
        event_store: EventStore,
        read_models: ReadModelStore,
        checkpoints: CheckpointStore,
        config: RunnerConfig | None = None,
    ) -> None:
        self._events = event_store
        self._read_models = read_models
        self._checkpoints = checkpoints
        self._config = config or RunnerConfig()
        self._projections: dict[str, Projection] = {}

    def register(self, projection: Projection) -> Projection:
        """Register ``projection`` (name must be unique). Returns it for chaining."""
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
        try:
            return self._projections[name]
        except KeyError as exc:
            raise UnknownProjectionError(name) from exc

    @property
    def read_models(self) -> ReadModelStore:
        """The shared read-model store (the read facades read through this)."""
        return self._read_models

    @property
    def checkpoint_store(self) -> CheckpointStore:
        return self._checkpoints

    @property
    def event_store(self) -> EventStore:
        return self._events

    def runner(self, name: str, *, namespace: str | None = None) -> ProjectionRunner:
        """A runner for one projection (default namespace = the projection's name)."""
        return ProjectionRunner(
            self.get(name),
            event_store=self._events,
            read_models=self._read_models,
            checkpoints=self._checkpoints,
            namespace=namespace,
            config=self._config,
        )

    async def catch_up_all(self) -> dict[str, CatchUpResult]:
        """Run a one-shot catch-up for every registered projection (cold start)."""
        results: dict[str, CatchUpResult] = {}
        for name in self.names():
            results[name] = await self.runner(name).catch_up()
        return results

    async def rebuild_projection(self, name: str) -> CatchUpResult:
        """Admin op: reset + replay one named projection from position 0.

        Raises :class:`UnknownProjectionError` if ``name`` is not registered.
        """
        if name not in self._projections:
            raise UnknownProjectionError(name)
        logger.info("rebuild_projection_requested", projection=name)
        return await self.runner(name).rebuild()

    async def rebuild_all(self) -> dict[str, CatchUpResult]:
        """Rebuild every registered projection from zero."""
        results: dict[str, CatchUpResult] = {}
        for name in self.names():
            results[name] = await self.runner(name).rebuild()
        return results

    async def checkpoints(self) -> list[ProjectionCheckpoint]:
        """The current checkpoint of every registered projection."""
        return [await self._checkpoints.load(name) for name in self.names()]

    async def lag(self) -> dict[str, int]:
        """Per-projection lag (head - checkpoint position), sharing one head read."""
        head = await self._events.last_position()
        out: dict[str, int] = {}
        for name in self.names():
            cp = await self._checkpoints.load(name)
            out[name] = max(0, head - cp.position)
        return out


def build_default_registry(
    *,
    event_store: EventStore,
    read_models: ReadModelStore | None = None,
    checkpoints: CheckpointStore | None = None,
    config: RunnerConfig | None = None,
) -> ProjectionRegistry:
    """Build a registry pre-loaded with the three product projections.

    ``read_models`` / ``checkpoints`` default to the in-memory implementations so
    a caller (or test) can spin up the whole read side over a real event store
    with no infrastructure. Production wires the Postgres-backed stores here.
    """
    from app.datalayer.readmodels import all_projections

    registry = ProjectionRegistry(
        event_store=event_store,
        read_models=read_models or InMemoryReadModelStore(),
        checkpoints=checkpoints or InMemoryCheckpointStore(),
        config=config,
    )
    registry.register_all(all_projections())  # type: ignore[arg-type]
    return registry


__all__ = [
    "ProjectionRegistry",
    "UnknownProjectionError",
    "build_default_registry",
]
