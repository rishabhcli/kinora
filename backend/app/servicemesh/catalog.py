"""Concrete Kinora inter-service message contracts (the seed registry).

This is where the abstract mesh meets the real system: it declares the structural
schemas for the messages that actually flow between the §12 roles and registers
them, with their channel compatibility contracts, into a :class:`SchemaRegistry`.
The shapes are *catalog-local* structural descriptions (declared via
:meth:`MessageSchema.from_fields`) — they mirror the real message surfaces without
importing the heavy DTOs, so this module stays cheap and infra-free, exactly like
the rest of the layer.

Three families, matching the three inter-service channels:

* **Queue jobs** (``render-worker`` drains them off Redis): ``shot.render.job`` —
  idempotent by ``shot_hash`` (§12.1).
* **Pub/sub events** (fanned out to readers / the director bar): ``shot.progress``
  and ``buffer.state`` (the §5.3 hairline override).
* **MCP calls** (the canon-memory server): ``canon.query`` / ``canon.query.result``.

:func:`build_seed_registry` returns a fully-populated registry; importing the module
does not touch global state.
"""

from __future__ import annotations

from app.servicemesh.compatibility import CompatibilityMode
from app.servicemesh.registry import SchemaRegistry
from app.servicemesh.schema import FieldSpec, FieldType, MessageSchema

__all__ = [
    "SHOT_RENDER_JOB",
    "SHOT_PROGRESS",
    "BUFFER_STATE",
    "CANON_QUERY",
    "CANON_QUERY_RESULT",
    "build_seed_registry",
    "seed_schemas",
]

SHOT_RENDER_JOB = "shot.render.job"
SHOT_PROGRESS = "shot.progress"
BUFFER_STATE = "buffer.state"
CANON_QUERY = "canon.query"
CANON_QUERY_RESULT = "canon.query.result"


def seed_schemas() -> list[tuple[MessageSchema, CompatibilityMode]]:
    """The seed schemas + the compatibility contract each channel is held to."""
    return [
        (
            MessageSchema.from_fields(
                SHOT_RENDER_JOB,
                "1.0.0",
                [
                    FieldSpec("shot_hash", FieldType.STRING),
                    FieldSpec("scene_id", FieldType.STRING),
                    FieldSpec("session_id", FieldType.STRING),
                    FieldSpec("priority", FieldType.INTEGER, required=False),
                    FieldSpec(
                        "render_mode",
                        FieldType.ENUM,
                        enum_values=frozenset({"live", "ken_burns", "card"}),
                    ),
                    FieldSpec("budget_seconds", FieldType.NUMBER, required=False),
                ],
                title="ShotRenderJob",
            ),
            CompatibilityMode.BACKWARD,
        ),
        (
            MessageSchema.from_fields(
                SHOT_PROGRESS,
                "1.0.0",
                [
                    FieldSpec("shot_hash", FieldType.STRING),
                    FieldSpec("session_id", FieldType.STRING),
                    FieldSpec(
                        "stage",
                        FieldType.ENUM,
                        enum_values=frozenset(
                            {"queued", "rendering", "muxing", "ready", "failed"}
                        ),
                    ),
                    FieldSpec("progress", FieldType.NUMBER, required=False),
                    FieldSpec("media_url", FieldType.STRING, required=False, nullable=True),
                ],
                title="ShotProgressEvent",
            ),
            CompatibilityMode.FULL,
        ),
        (
            MessageSchema.from_fields(
                BUFFER_STATE,
                "1.0.0",
                [
                    FieldSpec("session_id", FieldType.STRING),
                    FieldSpec("committed_seconds", FieldType.NUMBER),
                    FieldSpec("speculative_seconds", FieldType.NUMBER),
                    FieldSpec("hairline_page", FieldType.INTEGER, required=False),
                ],
                title="BufferStateEvent",
            ),
            CompatibilityMode.FULL,
        ),
        (
            MessageSchema.from_fields(
                CANON_QUERY,
                "1.0.0",
                [
                    FieldSpec("book_id", FieldType.STRING),
                    FieldSpec("tool", FieldType.STRING),
                    FieldSpec("arguments", FieldType.OBJECT, required=False),
                    FieldSpec("as_of_version", FieldType.INTEGER, required=False, nullable=True),
                ],
                title="CanonQuery",
            ),
            CompatibilityMode.BACKWARD,
        ),
        (
            MessageSchema.from_fields(
                CANON_QUERY_RESULT,
                "1.0.0",
                [
                    FieldSpec("ok", FieldType.BOOLEAN),
                    FieldSpec("result", FieldType.OBJECT, required=False),
                    FieldSpec("canon_version", FieldType.INTEGER, required=False),
                    FieldSpec("error", FieldType.STRING, required=False, nullable=True),
                ],
                title="CanonQueryResult",
            ),
            CompatibilityMode.BACKWARD,
        ),
    ]


def build_seed_registry() -> SchemaRegistry:
    """A fresh registry pre-loaded with the seed Kinora message contracts."""
    registry = SchemaRegistry()
    for schema, mode in seed_schemas():
        registry.register(schema, compatibility=mode)
    return registry
