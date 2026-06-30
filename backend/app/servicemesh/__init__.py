"""Service-mesh internal message/RPC contracts + schema versioning (§12).

Kinora's backend image runs as several roles off one codebase — ``api``,
``ingest-worker``, ``render-worker``, ``mcp`` — and they talk to each other over
three internal channels: the Redis priority queue (render/ingest jobs), pub/sub
(progress + buffer-state fan-out), and the canon-memory MCP server. Those messages
are a *contract*: a producer and a consumer that deploy independently must agree on
the shape and version of every message, and that agreement must survive schema
evolution without a flag-day redeploy.

This package is that contract layer. It is **additive and import-safe**: importing
it opens no sockets, touches no DB, and starts no event loop. The layering, bottom
to top:

* **errors** — the failure taxonomy every layer normalizes to.
* **versioning** — a tiny self-contained :class:`SemVer` + :class:`VersionRange`.
* **roles** — the §12 producer roles, transport kinds, content types.
* **schema** — structural :class:`MessageSchema` + deterministic content hashing.
* **envelope** — :class:`MessageEnvelope`: the versioned, trace/correlation/
  idempotency-stamped wrapper around every inter-service message.
* **compatibility** — backward/forward/full change classification + the CI gate
  that rejects a breaking change to a stable channel.
* **registry** — :class:`SchemaRegistry`: register schemas by semver + content
  hash, running the gate on every evolution.
* **converters** — :class:`ConverterRegistry`: a graph of adjacent migrators
  composed into shortest-path up/down conversion chains (graceful negotiation).
* **consumer** — :class:`ConsumerDispatcher`: validate + route by (id, version)
  with a :class:`DeadLetterQueue` for unhandlable messages.
* **negotiation** — capability/version handshake between two roles.
* **catalog** — the concrete Kinora message contracts + a seed registry.

Nothing here is wired into a running route or worker; it is the substrate those
call sites adopt incrementally.
"""

from __future__ import annotations

from app.servicemesh.catalog import (
    BUFFER_STATE,
    CANON_QUERY,
    CANON_QUERY_RESULT,
    SHOT_PROGRESS,
    SHOT_RENDER_JOB,
    build_seed_registry,
    seed_schemas,
)
from app.servicemesh.compatibility import (
    ChangeKind,
    CompatibilityMode,
    CompatibilityReport,
    SchemaChange,
    assert_evolution_allowed,
    check_compatibility,
    classify_changes,
)
from app.servicemesh.consumer import (
    ConsumerDispatcher,
    DeadLetter,
    DeadLetterQueue,
    DeadLetterReason,
    DispatchOutcome,
)
from app.servicemesh.converters import (
    ConverterRegistry,
    Direction,
    Migrator,
)
from app.servicemesh.envelope import MessageEnvelope, new_message_id, utc_now
from app.servicemesh.errors import (
    BreakingChangeError,
    CompatibilityError,
    ConversionError,
    DispatchError,
    EnvelopeDecodeError,
    NegotiationError,
    NoConversionPathError,
    SchemaAlreadyRegisteredError,
    SchemaError,
    SchemaHashMismatchError,
    SchemaNotFoundError,
    ServiceMeshError,
    UnhandledVersionError,
    UnknownSchemaError,
    VersionRangeError,
)
from app.servicemesh.negotiation import (
    Capability,
    CapabilityRegistry,
    NegotiationResult,
    RoleManifest,
    negotiate,
)
from app.servicemesh.registry import ChannelInfo, RegisteredSchema, SchemaRegistry
from app.servicemesh.roles import ContentType, ProducerRole, TransportKind
from app.servicemesh.schema import FieldSpec, FieldType, MessageSchema
from app.servicemesh.settings import ServiceMeshSettings, get_servicemesh_settings
from app.servicemesh.versioning import SemVer, VersionRange

__all__ = [
    # versioning
    "SemVer",
    "VersionRange",
    # roles
    "ProducerRole",
    "TransportKind",
    "ContentType",
    # schema
    "FieldSpec",
    "FieldType",
    "MessageSchema",
    # envelope
    "MessageEnvelope",
    "new_message_id",
    "utc_now",
    # compatibility
    "CompatibilityMode",
    "ChangeKind",
    "SchemaChange",
    "CompatibilityReport",
    "classify_changes",
    "check_compatibility",
    "assert_evolution_allowed",
    # registry
    "SchemaRegistry",
    "RegisteredSchema",
    "ChannelInfo",
    # converters
    "ConverterRegistry",
    "Migrator",
    "Direction",
    # consumer
    "ConsumerDispatcher",
    "DeadLetter",
    "DeadLetterQueue",
    "DeadLetterReason",
    "DispatchOutcome",
    # negotiation
    "Capability",
    "RoleManifest",
    "NegotiationResult",
    "negotiate",
    "CapabilityRegistry",
    # catalog
    "build_seed_registry",
    "seed_schemas",
    "SHOT_RENDER_JOB",
    "SHOT_PROGRESS",
    "BUFFER_STATE",
    "CANON_QUERY",
    "CANON_QUERY_RESULT",
    # settings
    "ServiceMeshSettings",
    "get_servicemesh_settings",
    # errors
    "ServiceMeshError",
    "SchemaError",
    "SchemaNotFoundError",
    "SchemaAlreadyRegisteredError",
    "SchemaHashMismatchError",
    "EnvelopeDecodeError",
    "CompatibilityError",
    "BreakingChangeError",
    "ConversionError",
    "NoConversionPathError",
    "DispatchError",
    "UnknownSchemaError",
    "UnhandledVersionError",
    "NegotiationError",
    "VersionRangeError",
]
