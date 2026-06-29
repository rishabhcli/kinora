"""Pure-unit tests for the event-store contracts, versioning, and serialization.

These run with **zero infrastructure** (no DB, no network) and pin the seam the
domain + projection facets depend on: the value objects, the ``ExpectedVersion``
algebra, metadata correlation/causation propagation, and the JSON serializer +
event-type registry (validation + upcasting).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.eventsourcing.store import (
    EventData,
    EventMetadata,
    EventTypeRegistry,
    JsonEventSerializer,
    OutboxRecord,
    OutboxStatus,
    RecordedEvent,
    Snapshot,
    StreamSlice,
)
from app.eventsourcing.store.errors import SerializationError
from app.eventsourcing.store.versioning import (
    ANY,
    NO_EVENTS,
    NO_STREAM,
    STREAM_EXISTS,
    StreamState,
    check,
    describe,
    is_satisfied,
    normalize,
)

# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #


def test_eventdata_requires_nonempty_type_and_dict_payload() -> None:
    with pytest.raises(ValueError):
        EventData(event_type="", payload={})
    with pytest.raises(ValueError):
        EventData(event_type="x", payload=[1, 2])  # type: ignore[arg-type]


def test_eventdata_defaults_unique_event_id() -> None:
    a = EventData(event_type="t", payload={})
    b = EventData(event_type="t", payload={})
    assert a.event_id != b.event_id
    assert len(a.event_id) == 32


def test_eventdata_is_immutable() -> None:
    from dataclasses import FrozenInstanceError

    e = EventData(event_type="t", payload={"k": 1})
    with pytest.raises(FrozenInstanceError):
        e.event_type = "u"  # type: ignore[misc]


def test_recorded_event_exposes_correlation_and_causation() -> None:
    meta = EventMetadata(correlation_id="corr", causation_id="cause", actor="adapter")
    rec = RecordedEvent(
        stream_id="s",
        event_id="e",
        event_type="t",
        version=0,
        global_position=1,
        payload={},
        metadata=meta,
        recorded_at=datetime.now(UTC),
    )
    assert rec.correlation_id == "corr"
    assert rec.causation_id == "cause"


def test_stream_slice_is_empty_helper() -> None:
    s = StreamSlice(stream_id="s", events=(), last_version=NO_EVENTS, is_end=True)
    assert s.is_empty
    assert s.last_version == -1


def test_snapshot_defaults() -> None:
    snap = Snapshot(stream_id="s", version=3, state={"x": 1})
    assert snap.snapshot_type == "default"
    assert snap.created_at.tzinfo is not None


def test_outbox_record_fields() -> None:
    now = datetime.now(UTC)
    rec = OutboxRecord(
        id="o1",
        event_id="e1",
        global_position=5,
        topic="canon",
        payload={"x": 1},
        status=OutboxStatus.PENDING,
        attempts=0,
        available_at=now,
        created_at=now,
    )
    assert rec.status is OutboxStatus.PENDING
    assert rec.published_at is None


# --------------------------------------------------------------------------- #
# Metadata envelope
# --------------------------------------------------------------------------- #


def test_metadata_round_trips_through_dict() -> None:
    meta = EventMetadata(
        correlation_id="c",
        causation_id="x",
        actor="scheduler",
        headers={"trace": "abc"},
    )
    d = meta.to_dict()
    assert d == {"trace": "abc", "correlation_id": "c", "causation_id": "x", "actor": "scheduler"}
    back = EventMetadata.from_dict(d)
    assert back == meta


def test_metadata_from_none_is_empty() -> None:
    assert EventMetadata.from_dict(None) == EventMetadata()


def test_caused_by_chains_correlation_and_causation() -> None:
    parent = RecordedEvent(
        stream_id="s",
        event_id="parent-id",
        event_type="t",
        version=0,
        global_position=1,
        payload={},
        metadata=EventMetadata(correlation_id="root-corr", actor="showrunner"),
        recorded_at=datetime.now(UTC),
    )
    child = EventMetadata(actor="adapter").caused_by(parent)
    assert child.causation_id == "parent-id"
    assert child.correlation_id == "root-corr"  # inherited from parent


def test_caused_by_falls_back_to_parent_event_id_for_correlation() -> None:
    parent = RecordedEvent(
        stream_id="s",
        event_id="parent-id",
        event_type="t",
        version=0,
        global_position=1,
        payload={},
        metadata=EventMetadata(),  # no correlation set
        recorded_at=datetime.now(UTC),
    )
    child = EventMetadata().caused_by(parent)
    assert child.correlation_id == "parent-id"


# --------------------------------------------------------------------------- #
# ExpectedVersion algebra
# --------------------------------------------------------------------------- #


def test_normalize_maps_no_events_int_to_no_stream() -> None:
    assert normalize(NO_EVENTS) is NO_STREAM


def test_normalize_rejects_bool_and_negative() -> None:
    with pytest.raises(ValueError):
        normalize(True)
    with pytest.raises(ValueError):
        normalize(-5)


def test_is_satisfied_any_always_true() -> None:
    assert is_satisfied(ANY, NO_EVENTS)
    assert is_satisfied(ANY, 99)


def test_is_satisfied_no_stream() -> None:
    assert is_satisfied(NO_STREAM, NO_EVENTS)
    assert not is_satisfied(NO_STREAM, 0)


def test_is_satisfied_stream_exists() -> None:
    assert not is_satisfied(STREAM_EXISTS, NO_EVENTS)
    assert is_satisfied(STREAM_EXISTS, 0)
    assert is_satisfied(STREAM_EXISTS, 7)


def test_is_satisfied_exact() -> None:
    assert is_satisfied(3, 3)
    assert not is_satisfied(3, 2)
    assert not is_satisfied(3, 4)


def test_check_raises_on_mismatch_with_context() -> None:
    from app.eventsourcing.store import OptimisticConcurrencyError

    with pytest.raises(OptimisticConcurrencyError) as exc:
        check("stream-1", 5, current_version=2)
    assert exc.value.stream_id == "stream-1"
    assert exc.value.actual == 2


def test_check_passes_when_satisfied() -> None:
    check("s", ANY, NO_EVENTS)  # no raise
    check("s", NO_STREAM, NO_EVENTS)
    check("s", 4, 4)


def test_describe_human_forms() -> None:
    assert describe(ANY) == "any"
    assert describe(NO_STREAM) == "no_stream"
    assert describe(7) == "version==7"


def test_streamstate_members() -> None:
    assert {s.value for s in StreamState} == {"any", "no_stream", "stream_exists"}


# --------------------------------------------------------------------------- #
# Serialization + registry
# --------------------------------------------------------------------------- #


def test_serializer_round_trips_plain_payload() -> None:
    ser = JsonEventSerializer()
    out = ser.serialize(EventData(event_type="t", payload={"a": 1, "b": [1, 2, 3]}))
    assert out == {"a": 1, "b": [1, 2, 3]}
    assert ser.deserialize("t", out) == out


def test_serializer_rejects_non_json_payload() -> None:
    ser = JsonEventSerializer()
    with pytest.raises(SerializationError):
        ser.serialize(EventData(event_type="t", payload={"when": datetime.now(UTC)}))


def test_registry_validation_runs_on_serialize() -> None:
    reg = EventTypeRegistry()

    def require_name(payload: dict) -> None:
        if "name" not in payload:
            raise ValueError("missing name")

    reg.register("canon.entity.upserted.v1", validator=require_name)
    ser = JsonEventSerializer(reg)

    ser.serialize(EventData(event_type="canon.entity.upserted.v1", payload={"name": "Elsa"}))
    with pytest.raises(SerializationError):
        ser.serialize(EventData(event_type="canon.entity.upserted.v1", payload={}))


def test_registry_upcasts_on_deserialize() -> None:
    reg = EventTypeRegistry()
    # v1 stored {"n": x}; current shape is {"name": x}.
    reg.register("legacy.v1", upcaster=lambda p: {"name": p.get("n")})
    ser = JsonEventSerializer(reg)
    assert ser.deserialize("legacy.v1", {"n": "Elsa"}) == {"name": "Elsa"}


def test_unregistered_type_passes_through() -> None:
    reg = EventTypeRegistry()
    ser = JsonEventSerializer(reg)
    payload = {"anything": True}
    assert ser.serialize(EventData(event_type="unknown", payload=payload)) == payload
    assert ser.deserialize("unknown", payload) == payload
    assert not reg.is_registered("unknown")


def test_registry_known_types() -> None:
    reg = EventTypeRegistry()
    reg.register("a.v1")
    reg.register("b.v1")
    assert reg.known_types() == frozenset({"a.v1", "b.v1"})
