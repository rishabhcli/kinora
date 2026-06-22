"""Enumerated column types shared across models.

Every enum is stored as a portable ``VARCHAR`` plus a named ``CHECK`` constraint
(``native_enum=False``) rather than a Postgres ``ENUM`` type. This keeps
migrations simple (no separate type to ``CREATE``/``ALTER``) and the values are
the lowercase strings from the design spec — :func:`str_enum` wires
``values_callable`` so the *value* (not the member name) is what hits the wire.
"""

from __future__ import annotations

import enum

from sqlalchemy import Enum as SAEnum


class BookStatus(enum.StrEnum):
    """Lifecycle of an imported book."""

    IMPORTING = "importing"
    READY = "ready"
    FAILED = "failed"


class EntityType(enum.StrEnum):
    """Kind of canon node."""

    CHARACTER = "character"
    LOCATION = "location"
    PROP = "prop"
    STYLE = "style"


class ShotStatus(enum.StrEnum):
    """Per-shot state machine (kinora.md §9.7)."""

    PLANNED = "planned"
    KEYFRAMED = "keyframed"
    PROMOTED = "promoted"
    RENDERING = "rendering"
    QA = "qa"
    ACCEPTED = "accepted"
    DEGRADED = "degraded"
    CONFLICT = "conflict"


class SessionMode(enum.StrEnum):
    """Who drives the workspace: the video (viewer) or the reader (director)."""

    VIEWER = "viewer"
    DIRECTOR = "director"


class RenderPriority(enum.StrEnum):
    """Render-queue lane (kinora.md §4.9)."""

    COMMITTED = "committed"
    SPECULATIVE = "speculative"
    KEYFRAME = "keyframe"


class RenderJobStatus(enum.StrEnum):
    """Render-job lifecycle in the priority queue (kinora.md §12.1)."""

    QUEUED = "queued"
    RESERVED = "reserved"
    SUBMITTED = "submitted"
    POLLING = "polling"
    SUCCEEDED = "succeeded"
    RETRYING = "retrying"
    CANCELLED = "cancelled"
    DEADLETTER = "deadletter"


def str_enum(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    """Build a VARCHAR+CHECK column type for ``enum_cls`` storing member values.

    Args:
        enum_cls: the Python :class:`enum.Enum` subclass.
        name: stable constraint name (feeds the ``ck_`` naming convention).
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [member.value for member in e],
    )
