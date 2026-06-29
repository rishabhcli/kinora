"""Unit tests for the domain-event framework: envelope (de)serialisation, the
registry, metadata provenance, and the upcaster framework. Pure; no store."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from app.eventsourcing.domain.events import (
    DomainEvent,
    EventMetadata,
    EventRegistry,
    UnknownEventTypeError,
    deserialise,
    serialise,
)
from app.eventsourcing.domain.upcasting import (
    MissingUpcasterError,
    UpcasterRegistry,
)


@dataclass(frozen=True, slots=True)
class _SampleEvent(DomainEvent):
    name: str = ""
    count: int = 0


def test_event_type_defaults_to_class_name() -> None:
    assert _SampleEvent.event_type == "_SampleEvent"
    assert _SampleEvent.schema_version == 1


def test_event_type_can_be_overridden() -> None:
    @dataclass(frozen=True, slots=True)
    class _Renamed(DomainEvent):
        event_type = "StableName"

    assert _Renamed.event_type == "StableName"


def test_serialise_round_trip() -> None:
    reg = EventRegistry()
    reg.register(_SampleEvent)
    event = _SampleEvent(name="hi", count=3)
    meta = EventMetadata(
        event_id="e1",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        actor_id="u1",
        correlation_id="corr",
    )
    envelope = serialise(event, meta)
    assert envelope["type"] == "_SampleEvent"
    assert envelope["version"] == 1
    assert envelope["data"] == {"name": "hi", "count": 3}
    meta_block = envelope["meta"]
    assert isinstance(meta_block, dict)
    assert meta_block["actor_id"] == "u1"

    back, back_meta = deserialise(envelope, event_registry=reg)
    assert back == event
    assert back_meta.event_id == "e1"
    assert back_meta.occurred_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert back_meta.actor_id == "u1"


def test_metadata_drops_none_fields() -> None:
    meta = EventMetadata(actor_id="u1")
    d = meta.to_dict()
    assert d == {"actor_id": "u1"}
    assert "occurred_at" not in d


def test_registry_rejects_conflicting_registration() -> None:
    reg = EventRegistry()
    reg.register(_SampleEvent)

    @dataclass(frozen=True, slots=True)
    class _Other(DomainEvent):
        event_type = "_SampleEvent"

    with pytest.raises(ValueError, match="already registered"):
        reg.register(_Other)


def test_registry_idempotent_for_same_class() -> None:
    reg = EventRegistry()
    reg.register(_SampleEvent)
    reg.register(_SampleEvent)  # same class, no error
    assert reg.resolve("_SampleEvent") is _SampleEvent


def test_deserialise_unknown_type_raises() -> None:
    reg = EventRegistry()
    with pytest.raises(UnknownEventTypeError):
        deserialise({"type": "Nope", "version": 1, "data": {}}, event_registry=reg)


# --------------------------------------------------------------------------- #
# Upcasting
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _VersionedEvent(DomainEvent):
    schema_version = 3
    full_name: str = ""
    tier: int = 0


def test_upcaster_chain_migrates_to_current() -> None:
    up = UpcasterRegistry()

    # v1 -> v2: rename "name" -> "full_name"
    @up.step("_VersionedEvent", 1)
    def _v1(data: Mapping[str, object]) -> dict[str, object]:
        d = dict(data)
        d["full_name"] = d.pop("name", "")
        return d

    # v2 -> v3: add defaulted "tier"
    @up.step("_VersionedEvent", 2)
    def _v2(data: Mapping[str, object]) -> dict[str, object]:
        d = dict(data)
        d.setdefault("tier", 0)
        return d

    reg = EventRegistry()
    reg.register(_VersionedEvent)

    old_envelope = {"type": "_VersionedEvent", "version": 1, "data": {"name": "Ada"}}
    event, _ = deserialise(old_envelope, event_registry=reg, upcasters=up)
    assert isinstance(event, _VersionedEvent)
    assert event.full_name == "Ada"
    assert event.tier == 0


def test_upcaster_missing_step_raises() -> None:
    up = UpcasterRegistry()
    up.register("_VersionedEvent", 1, lambda d: dict(d))  # only v1->v2, missing v2->v3
    reg = EventRegistry()
    reg.register(_VersionedEvent)
    with pytest.raises(MissingUpcasterError):
        deserialise(
            {"type": "_VersionedEvent", "version": 1, "data": {}},
            event_registry=reg,
            upcasters=up,
        )


def test_upcaster_noop_when_already_current() -> None:
    up = UpcasterRegistry()
    data = up.upcast("_VersionedEvent", 3, 3, {"full_name": "x", "tier": 1})
    assert data == {"full_name": "x", "tier": 1}


def test_upcaster_rejects_duplicate_step() -> None:
    up = UpcasterRegistry()
    up.register("E", 1, lambda d: dict(d))
    with pytest.raises(ValueError, match="already registered"):
        up.register("E", 1, lambda d: dict(d))


def test_upcaster_has_chain() -> None:
    up = UpcasterRegistry()
    up.register("E", 1, lambda d: dict(d))
    up.register("E", 2, lambda d: dict(d))
    assert up.has_chain("E", 1, 3) is True
    assert up.has_chain("E", 1, 4) is False
