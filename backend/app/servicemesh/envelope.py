"""The versioned message envelope wrapping every inter-service message.

A queue job, a pub/sub event, an MCP tool call, an RPC request — all of them ride
inside one :class:`MessageEnvelope`. The envelope carries the metadata that lets a
heterogeneous, independently-deployed fleet stay coherent:

* **schema_id + schema_version** — *what* this message is, and which revision of
  its shape, so the consumer can validate + convert (the registry key).
* **content_type** — how :pyattr:`payload` is serialized.
* **trace_id / span_id** — distributed-tracing correlation across hops.
* **correlation_id** — ties a whole logical flow together (e.g. one render of one
  shot across enqueue -> worker -> progress events).
* **causation_id** — the id of the message that *caused* this one (event-sourcing
  lineage).
* **idempotency_key** — the dedupe key (a render job keyed by ``shot_hash`` is a
  no-op on re-delivery, §12.1).
* **emitted_at** — producer wall-clock (UTC); injectable clock keeps tests
  deterministic.
* **producer_role** — which §12 role emitted it.

The envelope is a pydantic v2 model so it validates and serializes for free; it is
*generic over its payload only by convention* (the payload is an opaque mapping so
the envelope stays decoupled from any one DTO and can be parsed before the body's
schema is known).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.servicemesh.errors import EnvelopeDecodeError
from app.servicemesh.roles import ContentType, ProducerRole, TransportKind
from app.servicemesh.versioning import SemVer

__all__ = ["MessageEnvelope", "new_message_id", "utc_now"]

ENVELOPE_FORMAT_VERSION = 1
"""The version of the *envelope frame itself* (not the payload schema).

Bumped only if the envelope's own metadata shape changes; lets a consumer reject a
frame from a future mesh it cannot parse, distinct from payload-schema evolution.
"""


def new_message_id() -> str:
    """A fresh opaque message id (uuid4 hex)."""
    return uuid.uuid4().hex


def utc_now() -> datetime:
    """Timezone-aware UTC now (the default emitted-at clock)."""
    return datetime.now(UTC)


class MessageEnvelope(BaseModel):
    """A versioned, trace-correlated wrapper around an inter-service message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # -- envelope frame ----------------------------------------------------- #
    envelope_format: int = Field(default=ENVELOPE_FORMAT_VERSION, ge=1)
    message_id: str = Field(default_factory=new_message_id)

    # -- schema identity ---------------------------------------------------- #
    schema_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=5)  # serialized SemVer
    content_type: ContentType = ContentType.JSON
    transport: TransportKind = TransportKind.QUEUE_JOB

    # -- correlation / lineage ---------------------------------------------- #
    trace_id: str = Field(default_factory=new_message_id)
    span_id: str = Field(default_factory=new_message_id)
    correlation_id: str | None = None
    causation_id: str | None = None
    idempotency_key: str | None = None

    # -- provenance --------------------------------------------------------- #
    producer_role: ProducerRole = ProducerRole.UNKNOWN
    emitted_at: datetime = Field(default_factory=utc_now)

    # -- body --------------------------------------------------------------- #
    payload: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)

    # -- validators --------------------------------------------------------- #
    @field_validator("schema_version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        # Round-trips through the parser so a malformed version is rejected at the
        # boundary, never at dispatch time.
        return str(SemVer.parse(value))

    @field_validator("emitted_at")
    @classmethod
    def _ensure_tz(cls, value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    # -- convenience -------------------------------------------------------- #
    @property
    def version(self) -> SemVer:
        """The payload schema version as a :class:`SemVer`."""
        return SemVer.parse(self.schema_version)

    @property
    def registry_key(self) -> tuple[str, SemVer]:
        """The ``(schema_id, version)`` registry/dispatch key."""
        return (self.schema_id, self.version)

    @classmethod
    def create(
        cls,
        *,
        schema_id: str,
        schema_version: SemVer | str,
        payload: dict[str, Any],
        producer_role: ProducerRole = ProducerRole.UNKNOWN,
        transport: TransportKind = TransportKind.QUEUE_JOB,
        content_type: ContentType = ContentType.JSON,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
        trace_id: str | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> MessageEnvelope:
        """Stamp a fresh envelope around ``payload``.

        ``clock`` is injectable so tests get a deterministic ``emitted_at``.
        """
        return cls(
            schema_id=schema_id,
            schema_version=str(SemVer.coerce(schema_version)),
            payload=payload,
            producer_role=producer_role,
            transport=transport,
            content_type=content_type,
            correlation_id=correlation_id,
            causation_id=causation_id,
            idempotency_key=idempotency_key,
            headers=dict(headers or {}),
            trace_id=trace_id or new_message_id(),
            emitted_at=clock(),
        )

    def caused_child(
        self,
        *,
        schema_id: str,
        schema_version: SemVer | str,
        payload: dict[str, Any],
        producer_role: ProducerRole = ProducerRole.UNKNOWN,
        transport: TransportKind = TransportKind.PUBSUB_EVENT,
        clock: Callable[[], datetime] = utc_now,
    ) -> MessageEnvelope:
        """Derive a child envelope that propagates trace + correlation lineage.

        The child inherits this message's ``trace_id`` and ``correlation_id``
        (defaulting the correlation to this message id when none was set) and
        records this message as its ``causation_id`` — the event-sourcing chain.
        """
        return MessageEnvelope.create(
            schema_id=schema_id,
            schema_version=schema_version,
            payload=payload,
            producer_role=producer_role,
            transport=transport,
            correlation_id=self.correlation_id or self.message_id,
            causation_id=self.message_id,
            trace_id=self.trace_id,
            clock=clock,
        )

    # -- (de)serialization -------------------------------------------------- #
    def to_json(self) -> str:
        """Serialize to a JSON string (the wire form)."""
        return self.model_dump_json()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe mapping (queue/pubsub payload form)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_json(cls, raw: str | bytes) -> MessageEnvelope:
        """Parse a wire JSON string/bytes, wrapping any failure as a decode error."""
        try:
            return cls.model_validate_json(raw)
        except Exception as exc:  # noqa: BLE001 - normalize to the mesh taxonomy
            raise EnvelopeDecodeError(f"could not decode envelope JSON: {exc}") from exc

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MessageEnvelope:
        """Parse a mapping (e.g. a Redis hash), normalizing failures."""
        try:
            return cls.model_validate(data)
        except Exception as exc:  # noqa: BLE001 - normalize to the mesh taxonomy
            raise EnvelopeDecodeError(f"could not decode envelope mapping: {exc}") from exc
