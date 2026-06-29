"""The :class:`Projection` contract every read model implements.

A projection is a **pure-ish fold** of events into a read model: given an event
and a :class:`ReadModelStore`, it upserts/deletes the view rows that event
affects. The runtime (:mod:`app.eventsourcing.projections.runtime`) owns
delivery, checkpointing, ordering, and retries; the projection owns *only* the
"what does this event mean for my view" logic.

Two ways to author handlers:

1. Subclass :class:`Projection` and register per-type handlers with the
   :func:`handles` decorator (the ergonomic path the example projections use).
2. Implement :meth:`Projection.apply` directly for a fully custom dispatch.

The base class implements ``apply`` as a registry dispatch over the decorated
handlers, so subclasses normally never override it.

**Idempotency is a handler obligation, eased by the runtime.** The runtime
guarantees at-least-once delivery and dedupes already-applied ``event_id``\\ s
*per projection* (so a crash mid-batch cannot double-apply), but a handler
should still be written to be safe under replay — favour absolute set/upsert
("session X has read count N") over relative mutation ("increment by 1") where
the event carries enough state. Where only a delta is available, the runtime's
event-id dedupe is what makes the increment safe.

``interested_in`` lets a projection declare the event types it handles so the
runtime can ask the store for a *filtered* catch-up stream — a real throughput
win for a projection that cares about 2 of 200 types.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, TypeVar

from app.eventsourcing.projections.contracts import StoredEvent

if TYPE_CHECKING:
    from app.eventsourcing.projections.readmodel import ReadModelStore

# A handler takes (projection_self, store, namespace, event) and applies the
# event to the read model. Bound at dispatch time, so it is an unbound method.
Handler = Callable[..., Awaitable[None]]

ProjectionT = TypeVar("ProjectionT", bound="Projection")

# Sentinel registry attribute name set on the class by the decorator.
_REGISTRY_ATTR = "_kinora_projection_handlers"


def handles(*event_types: str) -> Callable[[Handler], Handler]:
    """Register a method as the handler for one or more event ``type`` strings.

    Usage::

        class SessionTimeline(Projection):
            name = "session_timeline"

            @handles("session.started")
            async def _on_started(self, store, namespace, event): ...

    The decorator tags the function; :meth:`Projection.__init_subclass__`
    collects every tagged method into the per-class dispatch table.
    """

    def decorate(fn: Handler) -> Handler:
        existing: tuple[str, ...] = getattr(fn, "_kinora_handles", ())
        fn._kinora_handles = existing + tuple(event_types)  # type: ignore[attr-defined]
        return fn

    return decorate


class Projection:
    """Base class for a read-model projection.

    Subclasses set :attr:`name` (the stable identity used for checkpoints and the
    default read-model namespace) and either decorate handlers with
    :func:`handles` or override :meth:`apply`.
    """

    #: Stable, unique name (checkpoint key + default namespace). Required.
    name: str = ""

    #: Bump when the fold logic changes incompatibly; a rebuild keys off this so
    #: a stale read model is detected and replayed rather than silently served.
    version: int = 1

    # Populated per-subclass by __init_subclass__ (type -> bound handler name).
    _kinora_projection_handlers: dict[str, str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        registry: dict[str, str] = {}
        # Inherit parents' handlers first so a subclass can extend a base.
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
        setattr(cls, _REGISTRY_ATTR, registry)

    @property
    def namespace(self) -> str:
        """The read-model namespace this projection writes to (default: its name)."""
        return self.name

    def interested_in(self) -> frozenset[str] | None:
        """Event types this projection handles, or ``None`` for "all events".

        Defaults to the set of decorated handler types. A projection that
        overrides :meth:`apply` to handle dynamic types should override this to
        return ``None`` (consume everything).
        """
        registry = getattr(type(self), _REGISTRY_ATTR, {})
        return frozenset(registry) if registry else None

    async def apply(
        self,
        store: ReadModelStore,
        namespace: str,
        event: StoredEvent,
    ) -> None:
        """Apply one event to the read model.

        The default implementation dispatches to the :func:`handles`-decorated
        method for ``event.type``; unknown types are ignored (a projection only
        reacts to what it declared). Override for fully custom dispatch.
        """
        registry: dict[str, str] = getattr(type(self), _REGISTRY_ATTR, {})
        handler_name = registry.get(event.type)
        if handler_name is None:
            return
        handler = getattr(self, handler_name)
        await handler(store, namespace, event)

    async def on_reset(self, store: ReadModelStore, namespace: str) -> None:
        """Hook called before a rebuild replays from position 0 (drop derived rows).

        The runtime clears the namespace for the slot being rebuilt; override only
        if a projection keeps state *outside* its namespace and must tear it down.
        """
        return None


def collect_handler_types(projection: Projection) -> Iterable[str]:
    """Every event type ``projection`` has a decorated handler for (for tests/introspection)."""
    return getattr(type(projection), _REGISTRY_ATTR, {}).keys()


def is_async_handler(fn: object) -> bool:
    """True if ``fn`` is an ``async def`` (used to validate registrations in tests)."""
    return inspect.iscoroutinefunction(fn)


__all__ = [
    "Handler",
    "Projection",
    "ProjectionT",
    "collect_handler_types",
    "handles",
    "is_async_handler",
]
