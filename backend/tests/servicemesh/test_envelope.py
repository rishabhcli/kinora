"""MessageEnvelope: round-trip, validation, lineage propagation, clock injection."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.servicemesh.envelope import MessageEnvelope
from app.servicemesh.errors import EnvelopeDecodeError, VersionRangeError
from app.servicemesh.roles import ContentType, ProducerRole, TransportKind
from app.servicemesh.versioning import SemVer

_FIXED = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)


def _fixed_clock() -> datetime:
    return _FIXED


def test_create_stamps_fields() -> None:
    env = MessageEnvelope.create(
        schema_id="shot.render.job",
        schema_version="1.2.0",
        payload={"shot_hash": "abc"},
        producer_role=ProducerRole.API,
        transport=TransportKind.QUEUE_JOB,
        idempotency_key="abc",
        clock=_fixed_clock,
    )
    assert env.schema_id == "shot.render.job"
    assert env.version == SemVer.parse("1.2.0")
    assert env.producer_role is ProducerRole.API
    assert env.idempotency_key == "abc"
    assert env.emitted_at == _FIXED
    assert env.content_type is ContentType.JSON
    assert env.registry_key == ("shot.render.job", SemVer.parse("1.2.0"))


def test_json_roundtrip() -> None:
    env = MessageEnvelope.create(
        schema_id="x.msg",
        schema_version="3.0.0",
        payload={"k": 1, "nested": {"a": [1, 2]}},
        producer_role=ProducerRole.RENDER_WORKER,
        clock=_fixed_clock,
    )
    again = MessageEnvelope.from_json(env.to_json())
    assert again == env
    assert again.payload == {"k": 1, "nested": {"a": [1, 2]}}


def test_dict_roundtrip() -> None:
    env = MessageEnvelope.create(
        schema_id="x.msg", schema_version="1.0.0", payload={"k": "v"}, clock=_fixed_clock
    )
    again = MessageEnvelope.from_dict(env.to_dict())
    assert again == env


def test_invalid_version_rejected_by_create() -> None:
    # create() coerces the version eagerly, so a malformed one fails fast with the
    # mesh's own VersionRangeError before pydantic is invoked.
    with pytest.raises(VersionRangeError):
        MessageEnvelope.create(
            schema_id="x", schema_version="not-a-version", payload={}, clock=_fixed_clock
        )


def test_invalid_version_rejected_by_model_validator() -> None:
    # Direct construction runs the field validator, which raises the mesh's own
    # VersionRangeError (pydantic propagates non-ValueError exceptions unwrapped).
    with pytest.raises(VersionRangeError):
        MessageEnvelope(schema_id="x", schema_version="not-a-version")


def test_too_short_version_is_a_validation_error() -> None:
    # The min_length frame constraint is a genuine pydantic validation failure.
    with pytest.raises(ValidationError):
        MessageEnvelope(schema_id="x", schema_version="1.0")


def test_decode_error_normalized() -> None:
    with pytest.raises(EnvelopeDecodeError):
        MessageEnvelope.from_json("{not json")
    with pytest.raises(EnvelopeDecodeError):
        MessageEnvelope.from_dict({"schema_id": "x"})  # missing required fields


def test_naive_emitted_at_is_made_aware() -> None:
    env = MessageEnvelope(
        schema_id="x",
        schema_version="1.0.0",
        emitted_at=datetime(2026, 1, 1, 0, 0, 0),  # naive
    )
    assert env.emitted_at.tzinfo is not None


def test_caused_child_propagates_lineage() -> None:
    parent = MessageEnvelope.create(
        schema_id="shot.render.job",
        schema_version="1.0.0",
        payload={"shot_hash": "h"},
        producer_role=ProducerRole.API,
        clock=_fixed_clock,
    )
    child = parent.caused_child(
        schema_id="shot.progress",
        schema_version="1.0.0",
        payload={"stage": "rendering"},
        producer_role=ProducerRole.RENDER_WORKER,
        clock=_fixed_clock,
    )
    # Trace continues; causation points at the parent; correlation defaults to the
    # parent's message id (it had none).
    assert child.trace_id == parent.trace_id
    assert child.causation_id == parent.message_id
    assert child.correlation_id == parent.message_id
    assert child.transport is TransportKind.PUBSUB_EVENT
    assert child.message_id != parent.message_id


def test_caused_child_keeps_existing_correlation() -> None:
    parent = MessageEnvelope.create(
        schema_id="a",
        schema_version="1.0.0",
        payload={},
        correlation_id="flow-123",
        clock=_fixed_clock,
    )
    child = parent.caused_child(
        schema_id="b", schema_version="1.0.0", payload={}, clock=_fixed_clock
    )
    assert child.correlation_id == "flow-123"


def test_envelope_is_frozen() -> None:
    env = MessageEnvelope.create(
        schema_id="x", schema_version="1.0.0", payload={}, clock=_fixed_clock
    )
    with pytest.raises(ValidationError):
        env.schema_id = "y"  # type: ignore[misc]
