"""Assemble the public schema: the Query, Mutation, and Subscription roots.

Wires the domain types + resolvers into the three operation roots and builds the
:class:`~app.graphql.schema.Schema` (which collects the reachable type registry).
Introspection (``__schema``/``__type``) is mixed into Query, and the
introspection meta-types are passed as ``extra_types`` so the registry includes
them. The assembled schema is a module-level singleton reused across requests.
"""

from __future__ import annotations

from app.graphql.introspection import introspection_query_fields
from app.graphql.root_types import IntrospectionTypes
from app.graphql.scalars import GraphQLID
from app.graphql.schema import Schema
from app.graphql.type_system import (
    Argument,
    Field,
    GraphQLNamedType,
    GraphQLNonNull,
    GraphQLObject,
    GraphQLScalar,
)
from app.graphql.types.connections import connection_type
from app.graphql.types.domain import (
    book_type,
    scene_type,
    session_type,
    shot_type,
)
from app.graphql.types.enums import BOOK_STATUS_ENUM
from app.graphql.types.meta import api_version_type, resolve_viewer, viewer_type
from app.graphql.types.mutations import (
    canon_edit_result_type,
    comment_result_type,
    conflict_result_type,
    create_session_input,
    director_comment_input,
    edit_canon_input,
    intent_result_type,
    resolve_conflict_input,
    seek_input,
    seek_result_type,
    update_intent_input,
)
from app.graphql.types.node import NODE_INTERFACE
from app.graphql.versioning import api_version_payload


def _build_query() -> GraphQLObject:
    from app.graphql.resolvers.book import resolve_book, resolve_books
    from app.graphql.resolvers.node import resolve_node
    from app.graphql.resolvers.scene import resolve_scene
    from app.graphql.resolvers.session import resolve_session
    from app.graphql.resolvers.shot import resolve_shot
    from app.graphql.scalars import GraphQLCursor

    return GraphQLObject(
        "Query",
        lambda: {
            "apiVersion": Field(
                GraphQLNonNull(api_version_type()),
                resolver=lambda s, a, c, i: api_version_payload(),
                cost=0,
                description="The published API version, stability, and deprecations.",
            ),
            "viewer": Field(
                GraphQLNonNull(viewer_type()),
                resolver=resolve_viewer,
                cost=0,
                description="The principal behind the presenting API key.",
            ),
            "node": Field(
                NODE_INTERFACE,
                args={"id": Argument(GraphQLNonNull(GraphQLID))},
                resolver=resolve_node,
                cost=2,
                description="Refetch any object by its opaque global id.",
            ),
            "book": Field(
                book_type(),
                args={"id": Argument(GraphQLNonNull(GraphQLID))},
                resolver=resolve_book,
                cost=2,
                required_scope="books:read",
            ),
            "books": Field(
                GraphQLNonNull(connection_type(book_type())),
                args={
                    "first": Argument(_int(), description="Forward page size (max 100)."),
                    "after": Argument(GraphQLCursor),
                    "last": Argument(_int()),
                    "before": Argument(GraphQLCursor),
                    "status": Argument(BOOK_STATUS_ENUM, description="Filter by status."),
                },
                resolver=resolve_books,
                cost=2,
                list_cost_multiplier=True,
                required_scope="books:read",
                description="The requesting owner's shelf, as a cursor connection.",
            ),
            "shot": Field(
                shot_type(),
                args={"id": Argument(GraphQLNonNull(GraphQLID))},
                resolver=resolve_shot,
                cost=2,
                required_scope="books:read",
            ),
            "scene": Field(
                scene_type(),
                args={"id": Argument(GraphQLNonNull(GraphQLID))},
                resolver=resolve_scene,
                cost=2,
                required_scope="books:read",
            ),
            "session": Field(
                session_type(),
                args={"id": Argument(GraphQLNonNull(GraphQLID))},
                resolver=resolve_session,
                cost=2,
                required_scope="sessions:read",
            ),
            **introspection_query_fields(lambda: _SCHEMA_HOLDER["schema"]),
        },
        description="The public read surface over the Kinora domain.",
    )


def _build_mutation() -> GraphQLObject:
    from app.graphql.resolvers.mutations import (
        resolve_create_session,
        resolve_director_comment,
        resolve_edit_canon,
        resolve_resolve_conflict,
        resolve_seek,
        resolve_update_intent,
    )

    return GraphQLObject(
        "Mutation",
        lambda: {
            "createReadingSession": Field(
                GraphQLNonNull(session_type()),
                args={"input": Argument(GraphQLNonNull(create_session_input()))},
                resolver=resolve_create_session,
                cost=5,
                required_scope="sessions:write",
            ),
            "updateIntent": Field(
                GraphQLNonNull(intent_result_type()),
                args={"input": Argument(GraphQLNonNull(update_intent_input()))},
                resolver=resolve_update_intent,
                cost=5,
                required_scope="sessions:write",
            ),
            "seek": Field(
                GraphQLNonNull(seek_result_type()),
                args={"input": Argument(GraphQLNonNull(seek_input()))},
                resolver=resolve_seek,
                cost=5,
                required_scope="sessions:write",
            ),
            "directorComment": Field(
                GraphQLNonNull(comment_result_type()),
                args={"input": Argument(GraphQLNonNull(director_comment_input()))},
                resolver=resolve_director_comment,
                cost=10,
                required_scope="director:write",
            ),
            "editCanon": Field(
                GraphQLNonNull(canon_edit_result_type()),
                args={"input": Argument(GraphQLNonNull(edit_canon_input()))},
                resolver=resolve_edit_canon,
                cost=10,
                required_scope="canon:write",
            ),
            "resolveConflict": Field(
                GraphQLNonNull(conflict_result_type()),
                args={"input": Argument(GraphQLNonNull(resolve_conflict_input()))},
                resolver=resolve_resolve_conflict,
                cost=10,
                required_scope="director:write",
            ),
        },
        description="The public write surface (sessions, director tools, canon).",
    )


def _build_subscription() -> GraphQLObject:
    from app.graphql.scalars import GraphQLJSON

    return GraphQLObject(
        "Subscription",
        {
            "sessionEvents": Field(
                GraphQLNonNull(GraphQLJSON),
                args={"sessionId": Argument(GraphQLNonNull(GraphQLID))},
                description="The §5.6 generation-event stream for a reading session.",
                required_scope="sessions:read",
                cost=2,
            )
        },
        description="Live generation events (bridged over SSE).",
    )


def _int() -> GraphQLScalar:
    from app.graphql.scalars import GraphQLInt

    return GraphQLInt


# Built lazily; the introspection roots need a back-reference to the schema, so we
# stage the holder, build the roots, then construct the schema and patch it in.
_SCHEMA_HOLDER: dict[str, Schema] = {}


def build_schema() -> Schema:
    """Build (once) and return the public schema singleton."""
    if "schema" in _SCHEMA_HOLDER:
        return _SCHEMA_HOLDER["schema"]
    # Placeholder so introspection_query_fields can close over the holder; the
    # real Schema replaces it immediately below before any request runs.
    query = GraphQLObject("Query", {"_": Field(GraphQLNonNull(api_version_type()))})
    placeholder = Schema(query=query)
    _SCHEMA_HOLDER["schema"] = placeholder
    extra: list[GraphQLNamedType] = list(IntrospectionTypes.get().values())
    schema = Schema(
        query=_build_query(),
        mutation=_build_mutation(),
        subscription=_build_subscription(),
        extra_types=extra,
    )
    _SCHEMA_HOLDER["schema"] = schema
    return schema


def get_schema() -> Schema:
    return build_schema()


__all__ = ["build_schema", "get_schema"]
