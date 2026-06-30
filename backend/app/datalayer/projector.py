"""The :class:`Projection` contract + the :class:`ProjectionRunner` that drives it.

A **projection** is a pure-ish fold of events into a read model: given a decoded
:class:`~app.datalayer.envelope.ProjectionEvent` and a
:class:`~app.datalayer.readmodel.ReadModelStore`, it upserts/deletes the rows that
event affects. The :class:`ProjectionRunner` owns everything operational —
catch-up over the global log, checkpointing, idempotent apply, and
rebuild-from-zero — so a projection only carries the "what does this event mean
for my view" logic.

Authoring handlers
------------------
Subclass :class:`Projection`, set :attr:`Projection.name`, and register per-type
handlers with :func:`handles`. The base :meth:`Projection.apply` dispatches by
``event.type``; unknown types are ignored. :meth:`Projection.interested_in`
defaults to the decorated handler types, letting the runner ask the store for a
*filtered* catch-up stream when a projection cares about a few of many types.

Idempotency
-----------
Delivery is at-least-once. The runner dedupes already-applied ``event_id``\\ s
per projection (so a crash mid-batch cannot double-apply), but handlers should
still favour absolute set/upsert ("book X has N accepted shots") over relative
mutation where the event carries enough state. Where only a delta is available,
the event-id dedupe is what makes the increment safe.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from app.core.logging import get_logger
from app.datalayer.checkpoints import (
    CheckpointStore,
    ProjectionCheckpoint,
    ProjectionStatus,
)
from app.datalayer.envelope import ProjectionEvent, decode
from app.datalayer.readmodel import ReadModelStore
from app.eventsourcing.store.contracts import EventStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

logger = get_logger("app.datalayer.projector")

Handler = Callable[..., Awaitable[None]]
ProjectionT = TypeVar("ProjectionT", bound="Projection")

_REGISTRY_ATTR = "_kinora_datalayer_handlers"


def handles(*event_types: str) -> Callable[[Handler], Handler]:
    """Register a method as the handler for one or more domain event ``type`` strings.

    Usage::

        class RenderProgress(Projection):
            name = "render_progress"

            @handles("ShotRendered")
            async def _on_rendered(self, store, namespace, event): ...
    """

    def decorate(fn: Handler) -> Handler:
        existing: tuple[str, ...] = getattr(fn, "_kinora_handles", ())
        fn._kinora_handles = existing + tuple(event_types)  # type: ignore[attr-defined]
        return fn

    return decorate


class Projection:
    """Base class for a read-model projection.

    Subclasses set :attr:`name` (the checkpoint key + default read-model
    namespace) and decorate handlers with :func:`handles` (or override
    :meth:`apply` for fully custom dispatch).
    """

    #: Stable, unique name (checkpoint key + default namespace). Required.
    name: str = ""

    #: Bump when the fold logic changes incompatibly; a rebuild keys off this so a
    #: stale read model is detected and replayed rather than silently served.
    version: int = 1

    _kinora_datalayer_handlers: dict[str, str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        registry: dict[str, str] = {}
        for base in reversed(cls.__mro__[1:]):
            parent = base.__dict__.get(_REGISTRY_ATTR)
            if parent:
                registry.update(parent)
        for attr_name, member in cls.__dict__.items():
            event_types: tuple[str, ...] = getattr(member, "_kinora_handles", ())
            for event_type in event_types:
                if event_type in registry and registry[event_type] != attr_name:
                    raise ValueError(
                        f"{cls.__name__}: duplicate handler for event type "
                        f"{event_type!r} ({registry[event_type]} and {attr_name})"
                    )
                registry[event_type] = attr_name
        cls._kinora_datalayer_handlers = registry

    @property
    def namespace(self) -> str:
        """The read-model namespace this projection writes to (default: its name)."""
        return self.name

    def interested_in(self) -> frozenset[str] | None:
        """Event types this projection handles, or ``None`` for "all events".

        Defaults to the decorated handler types. A projection overriding
        :meth:`apply` to handle dynamic types should override this to return
        ``None`` (consume everything).
        """
        registry = getattr(type(self), _REGISTRY_ATTR, {})
        return frozenset(registry) if registry else None

    async def apply(
        self,
        store: ReadModelStore,
        namespace: str,
        event: ProjectionEvent,
    ) -> None:
        """Apply one event to the read model (dispatch on ``event.type``)."""
        registry: dict[str, str] = getattr(type(self), _REGISTRY_ATTR, {})
        handler_name = registry.get(event.type)
        if handler_name is None:
            return
        handler = getattr(self, handler_name)
        await handler(store, namespace, event)

    async def on_reset(self, store: ReadModelStore, namespace: str) -> None:
        """Hook before a rebuild replays from zero (drop derived state outside the namespace).

        The runner clears the projection's namespace for you; override only if a
        projection keeps state elsewhere that must be torn down too.
        """
        return None


def collect_handler_types(projection: Projection) -> Iterable[str]:
    """Every event type ``projection`` has a decorated handler for (tests/introspection)."""
    return getattr(type(projection), _REGISTRY_ATTR, {}).keys()


def is_async_handler(fn: object) -> bool:
    """True if ``fn`` is an ``async def`` (used to validate registrations in tests)."""
    return inspect.iscoroutinefunction(fn)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    """Tuning for a :class:`ProjectionRunner`."""

    #: How many events ``read_all`` pages per catch-up batch.
    batch_size: int = 256


@dataclass(frozen=True, slots=True)
class CatchUpResult:
    """The outcome of a one-shot :meth:`ProjectionRunner.catch_up`."""

    projection: str
    applied: int
    skipped: int
    from_position: int
    to_position: int

    @property
    def advanced(self) -> bool:
        return self.to_position > self.from_position


class ProjectionRunner:
    """Drives one projection over a shared event store with checkpointed catch-up.

    The runner is the single operational entry point: :meth:`catch_up` pages the
    global log forward from the stored checkpoint, applies each event idempotently
    (skipping any already-applied ``event_id``), and advances the checkpoint as it
    goes; :meth:`rebuild` resets the projection (clears its namespace + applied
    set) and replays the entire log from position 0.

    It is deliberately store-agnostic: which concrete ``EventStore`` /
    ``ReadModelStore`` / ``CheckpointStore`` back it is the caller's choice
    (in-memory for tests, Postgres in production).
    """

    def __init__(
        self,
        projection: Projection,
        *,
        event_store: EventStore,
        read_models: ReadModelStore,
        checkpoints: CheckpointStore,
        namespace: str | None = None,
        config: RunnerConfig | None = None,
    ) -> None:
        if not projection.name:
            raise ValueError("Projection.name must be set")
        self._projection = projection
        self._events = event_store
        self._read_models = read_models
        self._checkpoints = checkpoints
        self._namespace = namespace or projection.namespace
        self._config = config or RunnerConfig()

    @property
    def name(self) -> str:
        return self._projection.name

    @property
    def namespace(self) -> str:
        return self._namespace

    async def checkpoint(self) -> ProjectionCheckpoint:
        return await self._checkpoints.load(self._projection.name)

    async def catch_up(self) -> CatchUpResult:
        """Page the log forward from the checkpoint, applying events idempotently.

        Reads ``read_all(from_position=position)`` (exclusive) in batches until the
        store reports no more events. Each event is folded **idempotently**: it is
        skipped if its ``event_id`` is already in the applied ledger, otherwise the
        handler runs and *then* the id is marked applied — apply-before-mark, so a
        handler that raises leaves the event unapplied (retriable) rather than
        poisoning the ledger. The checkpoint advances after **each** event so a
        crash never replays more than the unapplied tail. Returns a per-call
        summary; a handler exception faults the checkpoint and re-raises.
        """
        name = self._projection.name
        cp = await self._checkpoints.load(name)
        start = cp.position
        applied = 0
        skipped = 0
        position = start
        # Sync the recorded fold version so a version bump is observable to ops.
        if cp.projection_version != self._projection.version:
            await self._checkpoints.set_projection_version(name, self._projection.version)

        head = await self._events.last_position()
        while True:
            batch = await self._events.read_all(
                from_position=position, limit=self._config.batch_size
            )
            if not batch:
                break
            for recorded in batch:
                event = decode(recorded)
                # Dedupe check FIRST: if a previous run already folded this event,
                # skip it. Apply BEFORE marking applied so a *failing* handler does
                # not poison the ledger — the event stays unapplied and is retried.
                already = await self._checkpoints.was_applied(name, event.event_id)
                if already:
                    skipped += 1
                else:
                    try:
                        await self._apply_one(event)
                    except Exception as exc:  # noqa: BLE001 - surface via checkpoint
                        await self._checkpoints.record_error(name, repr(exc))
                        logger.error(
                            "projection_apply_failed",
                            projection=name,
                            event_id=event.event_id,
                            event_type=event.type,
                            global_position=event.global_position,
                            error=repr(exc),
                        )
                        raise
                    await self._checkpoints.mark_applied(name, event.event_id)
                    applied += 1
                position = recorded.global_position
                await self._checkpoints.advance(
                    name,
                    position,
                    applied_delta=0 if already else 1,
                    observed_head=max(head, position),
                )
            if len(batch) < self._config.batch_size:
                break

        await self._checkpoints.advance(
            name,
            position,
            status=ProjectionStatus.LIVE,
            observed_head=max(head, position),
        )
        return CatchUpResult(
            projection=name,
            applied=applied,
            skipped=skipped,
            from_position=start,
            to_position=position,
        )

    async def rebuild(self) -> CatchUpResult:
        """Rebuild from zero: clear the read model + applied set, then replay all.

        Drops every row in the projection's namespace, invokes
        :meth:`Projection.on_reset`, resets the checkpoint to position 0 (which
        clears the applied-event dedupe set), and runs a full :meth:`catch_up`.
        Idempotent: a rebuild followed by another rebuild yields the same view.
        """
        name = self._projection.name
        await self._read_models.clear(self._namespace)
        await self._projection.on_reset(self._read_models, self._namespace)
        await self._checkpoints.reset(name)
        await self._checkpoints.set_projection_version(name, self._projection.version)
        result = await self.catch_up()
        logger.info(
            "projection_rebuilt",
            projection=name,
            namespace=self._namespace,
            applied=result.applied,
            to_position=result.to_position,
        )
        return result

    async def _apply_one(self, event: ProjectionEvent) -> None:
        await self._projection.apply(self._read_models, self._namespace, event)


async def replay_into(
    projection: Projection,
    events: Sequence[ProjectionEvent],
    read_models: ReadModelStore,
    *,
    namespace: str | None = None,
) -> None:
    """Apply a sequence of already-decoded events to ``read_models`` in order.

    A dependency-free helper the consistency checker uses to materialise a fresh
    view from raw events without a checkpoint store (the rebuild it compares
    against). Order is the caller's responsibility (pass events in global order).
    """
    ns = namespace or projection.namespace
    for event in events:
        await projection.apply(read_models, ns, event)


__all__ = [
    "CatchUpResult",
    "Handler",
    "Projection",
    "ProjectionRunner",
    "ProjectionT",
    "RunnerConfig",
    "collect_handler_types",
    "handles",
    "is_async_handler",
    "replay_into",
]
