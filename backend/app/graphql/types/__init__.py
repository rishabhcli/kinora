"""The Kinora public domain types for the GraphQL schema.

Each module here builds one slice of the public type system (enums, the ``Node``
interface, books/pages, shots, scenes/films, sessions, canon, prefs, viewer,
mutation results) using the code-built type system in
``app/graphql/type_system.py``. The concrete object types are cached as
singletons so the schema (assembled in ``app/graphql/root.py``) shares the same
instances and the registry stays consistent.
"""

from __future__ import annotations

from app.graphql.types.enums import (
    BOOK_STATUS_ENUM,
    ENTITY_TYPE_ENUM,
    RENDER_PRIORITY_ENUM,
    SESSION_MODE_ENUM,
    SHOT_STATUS_ENUM,
)

__all__ = [
    "BOOK_STATUS_ENUM",
    "ENTITY_TYPE_ENUM",
    "RENDER_PRIORITY_ENUM",
    "SESSION_MODE_ENUM",
    "SHOT_STATUS_ENUM",
]
