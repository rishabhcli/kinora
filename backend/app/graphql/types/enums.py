"""Public GraphQL enums mirroring the domain's ``StrEnum`` columns.

The public names are the SCREAMING_SNAKE GraphQL convention; the internal values
are the lowercase strings the DB stores (``ShotStatus.ACCEPTED == "accepted"``),
so an enum's ``parse_value``/``serialize`` round-trips through the same string the
repositories use. They are defined independently of the SQLAlchemy enums so the
public contract is stable even if an internal enum gains a member that is not yet
part of the public API.
"""

from __future__ import annotations

import enum

from app.db.models.enums import (
    BookStatus,
    EntityType,
    RenderPriority,
    SessionMode,
    ShotStatus,
)
from app.graphql.type_system import EnumValue, GraphQLEnum


def _from_strenum(name: str, enum_cls: type[enum.Enum], description: str) -> GraphQLEnum:
    """Build a public GraphQLEnum from a domain ``StrEnum`` (UPPER name → value)."""
    values = [EnumValue(member.name, member.value) for member in enum_cls]
    return GraphQLEnum(name, values, description=description)


BOOK_STATUS_ENUM = _from_strenum(
    "BookStatus", BookStatus, "Import lifecycle of a book on the shelf."
)
SHOT_STATUS_ENUM = _from_strenum(
    "ShotStatus", ShotStatus, "Per-shot state machine status (kinora.md §9.7)."
)
ENTITY_TYPE_ENUM = _from_strenum(
    "EntityType", EntityType, "Kind of canon node: character, location, prop, or style."
)
SESSION_MODE_ENUM = _from_strenum(
    "SessionMode", SessionMode, "Who drives the workspace: viewer or director."
)
RENDER_PRIORITY_ENUM = _from_strenum(
    "RenderPriority", RenderPriority, "Render-queue lane for a job (kinora.md §4.9)."
)

CONFLICT_OPTION_ENUM = GraphQLEnum(
    "ConflictOption",
    [
        EnumValue("HONOR_CANON", "honor_canon"),
        EnumValue("EVOLVE_CANON", "evolve_canon"),
        EnumValue("SURFACE_TO_USER", "surface_to_user"),
    ],
    description="The Director's resolution of a surfaced continuity conflict (§7.2).",
)


__all__ = [
    "BOOK_STATUS_ENUM",
    "CONFLICT_OPTION_ENUM",
    "ENTITY_TYPE_ENUM",
    "RENDER_PRIORITY_ENUM",
    "SESSION_MODE_ENUM",
    "SHOT_STATUS_ENUM",
]
