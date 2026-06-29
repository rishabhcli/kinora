"""Event payload serialization + a logical event-type registry.

The store persists payloads as JSONB, so "serialization" here is mostly
*validation and JSON-safety*: confirm the payload round-trips through JSON and,
when a schema is registered for the event type, that it conforms.

:class:`EventTypeRegistry` lets a facet register an event type once with an
optional validator and an optional upcaster (a function that migrates an older
stored shape forward to the current one on read). This is how event-sourcing
systems evolve a schema without rewriting history: old events stay on disk in
their original shape and are upcast lazily when replayed.

Everything here is pure; no I/O.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.eventsourcing.store.contracts import EventData
from app.eventsourcing.store.errors import SerializationError

#: A validator returns ``None`` on success or raises / returns a reason string.
Validator = Callable[[dict[str, Any]], None]
#: An upcaster migrates a stored payload forward to the current shape.
Upcaster = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class EventTypeSpec:
    """Registration record for one logical event type."""

    event_type: str
    validator: Validator | None = None
    upcaster: Upcaster | None = None


class EventTypeRegistry:
    """A registry of known event types with optional validators + upcasters.

    Registration is optional: an *unregistered* type is accepted as-is (the store
    is intentionally permissive so the domain facet can evolve its event
    vocabulary without first touching the store). Registering a type adds
    validation on write and upcasting on read.
    """

    def __init__(self) -> None:
        self._specs: dict[str, EventTypeSpec] = {}

    def register(
        self,
        event_type: str,
        *,
        validator: Validator | None = None,
        upcaster: Upcaster | None = None,
    ) -> None:
        """Register (or replace) the spec for ``event_type``."""
        if not event_type:
            raise ValueError("event_type must be non-empty")
        self._specs[event_type] = EventTypeSpec(event_type, validator, upcaster)

    def is_registered(self, event_type: str) -> bool:
        return event_type in self._specs

    def known_types(self) -> frozenset[str]:
        return frozenset(self._specs)

    def validate(self, event_type: str, payload: dict[str, Any]) -> None:
        spec = self._specs.get(event_type)
        if spec is None or spec.validator is None:
            return
        try:
            spec.validator(payload)
        except SerializationError:
            raise
        except Exception as exc:  # validator may raise anything
            raise SerializationError(
                f"payload for {event_type!r} failed validation: {exc}"
            ) from exc

    def upcast(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        spec = self._specs.get(event_type)
        if spec is None or spec.upcaster is None:
            return payload
        try:
            migrated = spec.upcaster(payload)
        except Exception as exc:
            raise SerializationError(
                f"upcasting {event_type!r} failed: {exc}"
            ) from exc
        if not isinstance(migrated, dict):
            raise SerializationError(f"upcaster for {event_type!r} did not return a dict")
        return migrated


def _assert_json_safe(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Round-trip ``payload`` through JSON, raising on a non-serialisable value.

    Doing this at the store boundary turns a latent "the DB rejected this JSONB"
    failure into a clear :class:`SerializationError` at append time, with the
    offending event type named.
    """
    try:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise SerializationError(
            f"payload for {event_type!r} is not JSON-serialisable: {exc}"
        ) from exc
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise SerializationError(f"payload for {event_type!r} must be a JSON object")
    return decoded


class JsonEventSerializer:
    """The default :class:`~contracts.EventSerializer`: JSON-safety + registry.

    On write it validates against the registry (if registered) and asserts JSON
    safety. On read it applies the registry's upcaster (if any). Holding a shared
    registry instance means both arms see the same schema knowledge.
    """

    def __init__(self, registry: EventTypeRegistry | None = None) -> None:
        self.registry = registry or EventTypeRegistry()

    def serialize(self, event: EventData) -> dict[str, Any]:
        self.registry.validate(event.event_type, event.payload)
        return _assert_json_safe(event.event_type, event.payload)

    def deserialize(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.registry.upcast(event_type, payload)


__all__ = [
    "EventTypeRegistry",
    "EventTypeSpec",
    "JsonEventSerializer",
    "Upcaster",
    "Validator",
]
