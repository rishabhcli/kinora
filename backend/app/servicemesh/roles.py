"""Producer roles, transport kinds, and content types for the mesh envelope.

Kinora's backend image runs as several *roles* off one codebase (AGENTS.md / §12
process model): the ``api`` server, the ``ingest-worker``, the ``render-worker``,
and the ``mcp`` canon server. Every message on an internal channel is stamped with
the role that produced it so a consumer can attribute, route, and (for negotiation)
discover who speaks which schema version.

These are plain ``StrEnum`` so they serialize to readable strings on the wire and
compare equal to their string form — no custom codec needed.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["ProducerRole", "TransportKind", "ContentType"]


class ProducerRole(StrEnum):
    """The backend role that emitted a message (mirrors the §12 process model)."""

    API = "api"
    INGEST_WORKER = "ingest-worker"
    RENDER_WORKER = "render-worker"
    MCP = "mcp"
    SCHEDULER = "scheduler"
    UNKNOWN = "unknown"


class TransportKind(StrEnum):
    """Which inter-service channel an envelope rides on.

    The envelope is transport-agnostic, but stamping the *intended* channel lets
    the consumer dispatcher and observability distinguish a Redis queue job from a
    pub/sub broadcast from a synchronous MCP tool call.
    """

    QUEUE_JOB = "queue.job"  # Redis priority-queue render/ingest job
    PUBSUB_EVENT = "pubsub.event"  # fan-out progress / buffer-state event
    MCP_CALL = "mcp.call"  # canon-memory MCP tool invocation
    MCP_RESULT = "mcp.result"  # the paired MCP tool result
    RPC_REQUEST = "rpc.request"  # in-process / cross-process RPC request
    RPC_RESPONSE = "rpc.response"  # the paired RPC response


class ContentType(StrEnum):
    """Serialization of the envelope payload (the ``content_type`` field)."""

    JSON = "application/json"
    # Reserved for future binary codecs; declared so consumers can reject early.
    MSGPACK = "application/msgpack"
    PROTOBUF = "application/protobuf"
