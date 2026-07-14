"""Domain events, their envelope, and the (de)serialisation registry.

A **domain event** is an immutable fact that already happened: ``SessionStarted``,
``ShotRendered``, ``CanonFieldEdited``. Aggregates *decide* and *emit* these; they
are the only thing ever written to the store, and replaying them is the only way
an aggregate rebuilds its state.

Every event is a frozen dataclass deriving from :class:`DomainEvent`. The class
declares two pieces of identity:

* ``event_type`` — a stable string discriminator (defaults to the class name);
* ``schema_version`` — an ``int`` that bumps whenever the event's *stored shape*
  changes, so the :mod:`app.eventsourcing.domain.upcasting` framework can migrate
  old rows forward on load.

The wire form is a small **envelope**::

    {"type": "SessionStarted", "version": 1, "data": {...}, "meta": {...}}

``data`` is the event's own fields; ``meta`` is :class:`EventMetadata`
(causation/correlation/actor/occurred_at). The :class:`EventRegistry` maps a
``type`` back to its class so :func:`deserialise` can reconstruct it, running any
registered upcasters first.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, ClassVar, TypeVar


@dataclass(frozen=True, slots=True)
class EventMetadata:
    """Provenance carried alongside every event (the envelope's ``meta`` block).

    Attributes:
        event_id: a unique id for this event occurrence (set by the bus on commit).
        occurred_at: when the decision produced the event (UTC).
        actor_id: who/what caused it — a user id, an agent name, or ``"system"``.
        correlation_id: groups every event produced while handling one originating
            request/intent (so a render triggered by a comment shares the id).
        causation_id: the id of the command (or upstream event) that *directly*
            caused this event — one hop, for tracing the chain.
        tenant_id: optional workspace/tenant for isolation (mirrors the auth seam).
    """

    event_id: str | None = None
    occurred_at: datetime | None = None
    actor_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    tenant_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for key, value in dataclasses.asdict(self).items():
            if value is None:
                continue
            out[key] = value.isoformat() if isinstance(value, datetime) else value
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> EventMetadata:
        occurred_raw = raw.get("occurred_at")
        occurred = datetime.fromisoformat(occurred_raw) if isinstance(occurred_raw, str) else None
        return cls(
            event_id=_opt_str(raw.get("event_id")),
            occurred_at=occurred,
            actor_id=_opt_str(raw.get("actor_id")),
            correlation_id=_opt_str(raw.get("correlation_id")),
            causation_id=_opt_str(raw.get("causation_id")),
            tenant_id=_opt_str(raw.get("tenant_id")),
        )


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """Base class for all domain events — an immutable, replayable fact.

    Subclasses are ``@dataclass(frozen=True, slots=True)`` and add their own
    fields. They override :attr:`event_type` only when the stored discriminator
    must differ from the class name (e.g. for a renamed class), and bump
    :attr:`schema_version` whenever the persisted ``data`` shape changes.
    """

    #: Stable discriminator. Defaults to the class name in ``__init_subclass__``.
    event_type: ClassVar[str] = "DomainEvent"
    #: Bumped whenever the persisted shape of this event changes (upcaster key).
    schema_version: ClassVar[int] = 1

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # dataclass(slots=True) replaces the class object, so zero-argument
        # super() would retain the pre-replacement __class__ cell.
        super(DomainEvent, cls).__init_subclass__(**kwargs)
        # Default the discriminator to the class name unless explicitly set on the
        # subclass body (not merely inherited from a parent event class).
        if "event_type" not in cls.__dict__:
            cls.event_type = cls.__name__

    def to_data(self) -> dict[str, object]:
        """The event's own fields as a JSON-ready mapping (the envelope ``data``)."""
        out: dict[str, object] = {}
        for f in fields(self):
            out[f.name] = _encode(getattr(self, f.name))
        return out

    @classmethod
    def from_data(cls: type[_E], data: Mapping[str, object]) -> _E:
        """Reconstruct from a (possibly upcasted) ``data`` mapping.

        The default implementation feeds the mapping straight to the constructor,
        picking only the declared fields, so an upcaster that *adds* a defaulted
        field or *renames* one keeps this total. Events with non-trivial field
        types override this.
        """
        names = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in names}
        return cls(**kwargs)


_E = TypeVar("_E", bound=DomainEvent)


def _encode(value: object) -> object:
    """JSON-ready encoding for the small set of field types events use."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _encode(v) for k, v in value.items()}
    return value


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


class EventRegistry:
    """Maps an event ``type`` string to its class for deserialisation.

    Each domain module registers its events at import time via
    :func:`register_events`. The registry is the single place that knows the
    current :attr:`DomainEvent.schema_version` per type, which the upcaster
    framework targets.
    """

    def __init__(self) -> None:
        self._by_type: dict[str, type[DomainEvent]] = {}

    def register(self, event_cls: type[DomainEvent]) -> type[DomainEvent]:
        key = event_cls.event_type
        existing = self._by_type.get(key)
        if existing is not None and existing is not event_cls:
            raise ValueError(f"event type {key!r} already registered to {existing.__name__}")
        self._by_type[key] = event_cls
        return event_cls

    def resolve(self, event_type: str) -> type[DomainEvent]:
        try:
            return self._by_type[event_type]
        except KeyError as exc:
            raise UnknownEventTypeError(event_type) from exc

    def current_version(self, event_type: str) -> int:
        return self.resolve(event_type).schema_version

    def known_types(self) -> frozenset[str]:
        return frozenset(self._by_type)


class UnknownEventTypeError(KeyError):
    """Raised when deserialising an event whose ``type`` was never registered."""

    def __init__(self, event_type: str) -> None:
        self.event_type = event_type
        super().__init__(event_type)


#: The process-wide registry every domain module registers into.
registry = EventRegistry()


def register_events(*event_classes: type[DomainEvent]) -> None:
    """Register one or more event classes into the default :data:`registry`."""
    for cls in event_classes:
        registry.register(cls)


# --------------------------------------------------------------------------- #
# Envelope (de)serialisation
# --------------------------------------------------------------------------- #


def serialise(event: DomainEvent, metadata: EventMetadata | None = None) -> dict[str, object]:
    """Render an event + metadata to the on-the-wire envelope the store appends."""
    return {
        "type": event.event_type,
        "version": event.schema_version,
        "data": event.to_data(),
        "meta": (metadata or EventMetadata()).to_dict(),
    }


def deserialise(
    envelope: Mapping[str, object],
    *,
    event_registry: EventRegistry = registry,
    upcasters: UpcasterRegistry | None = None,
) -> tuple[DomainEvent, EventMetadata]:
    """Reconstruct an ``(event, metadata)`` pair from a stored envelope.

    Runs the registered upcaster chain (if any) to migrate the stored ``data``
    from its ``version`` up to the type's current :attr:`schema_version` before
    constructing the event.

    Raises:
        UnknownEventTypeError: the ``type`` is not in ``event_registry``.
    """
    event_type = str(envelope["type"])
    raw_version = envelope.get("version", 1)
    stored_version = int(raw_version) if isinstance(raw_version, (int, str)) else 1
    raw_data = envelope.get("data", {})
    data: dict[str, object] = dict(raw_data) if isinstance(raw_data, Mapping) else {}
    cls = event_registry.resolve(event_type)
    target = cls.schema_version
    if upcasters is not None and stored_version < target:
        data = upcasters.upcast(event_type, stored_version, target, data)
    event = cls.from_data(data)
    raw_meta = envelope.get("meta", {})
    meta_map: Mapping[str, object] = raw_meta if isinstance(raw_meta, Mapping) else {}
    metadata = EventMetadata.from_dict(meta_map)
    return event, metadata


def now_utc() -> datetime:
    """The current UTC time — the one clock the write side reads (injectable)."""
    return datetime.now(UTC)


# Imported lazily at the bottom to avoid a cycle: upcasting imports nothing from
# here at module scope, but the type hint above references it.
from app.eventsourcing.domain.upcasting import UpcasterRegistry  # noqa: E402


@dataclass(frozen=True, slots=True)
class PendingEvent:
    """An event an aggregate decided to emit but has not yet committed.

    Pairs the event with the partial metadata the aggregate knows (actor it was
    told about); the command bus fills the rest (event_id, occurred_at,
    causation/correlation) at commit time.
    """

    event: DomainEvent
    metadata: EventMetadata = field(default_factory=EventMetadata)


__all__ = [
    "DomainEvent",
    "EventMetadata",
    "EventRegistry",
    "PendingEvent",
    "UnknownEventTypeError",
    "deserialise",
    "now_utc",
    "register_events",
    "registry",
    "serialise",
]
