"""Relay connection object types built generically for any node type.

For a node object ``T`` this produces the ``TEdge`` and ``TConnection`` object
types and the shared ``PageInfo`` type, with default resolvers that read the
:class:`~app.graphql.pagination.Connection`/``Edge``/``PageInfo`` dataclasses
produced by ``connection_from_list``. Connections are cached per node type so the
schema registry shares one instance.
"""

from __future__ import annotations

from app.graphql.scalars import GraphQLBoolean, GraphQLCursor, GraphQLInt
from app.graphql.type_system import (
    Field,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObject,
)

_PAGE_INFO: GraphQLObject | None = None
_CONNECTION_CACHE: dict[str, GraphQLObject] = {}


def page_info_type() -> GraphQLObject:
    """The shared ``PageInfo`` object type (built once)."""
    global _PAGE_INFO
    if _PAGE_INFO is None:
        _PAGE_INFO = GraphQLObject(
            "PageInfo",
            {
                "hasNextPage": Field(
                    GraphQLNonNull(GraphQLBoolean),
                    resolver=lambda s, a, c, i: s.has_next_page,
                    description="Whether more edges exist after `endCursor`.",
                ),
                "hasPreviousPage": Field(
                    GraphQLNonNull(GraphQLBoolean),
                    resolver=lambda s, a, c, i: s.has_previous_page,
                    description="Whether edges exist before `startCursor`.",
                ),
                "startCursor": Field(
                    GraphQLCursor,
                    resolver=lambda s, a, c, i: s.start_cursor,
                ),
                "endCursor": Field(
                    GraphQLCursor,
                    resolver=lambda s, a, c, i: s.end_cursor,
                ),
            },
            description="Relay-style pagination metadata for a connection.",
        )
    return _PAGE_INFO


def connection_type(node_type: GraphQLObject) -> GraphQLObject:
    """The ``<Node>Connection`` object type for ``node_type`` (cached)."""
    name = f"{node_type.name}Connection"
    if name in _CONNECTION_CACHE:
        return _CONNECTION_CACHE[name]
    edge = GraphQLObject(
        f"{node_type.name}Edge",
        {
            "node": Field(
                GraphQLNonNull(node_type),
                resolver=lambda s, a, c, i: s.node,
                description="The item at the end of this edge.",
            ),
            "cursor": Field(
                GraphQLNonNull(GraphQLCursor),
                resolver=lambda s, a, c, i: s.cursor,
                description="An opaque cursor for paginating after this edge.",
            ),
        },
        description=f"An edge in a {node_type.name} connection.",
    )
    connection = GraphQLObject(
        name,
        lambda: {
            "edges": Field(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(edge))),
                resolver=lambda s, a, c, i: s.edges,
                description="The list of edges in this page.",
            ),
            "pageInfo": Field(
                GraphQLNonNull(page_info_type()),
                resolver=lambda s, a, c, i: s.page_info,
                description="Pagination metadata for this page.",
            ),
            "totalCount": Field(
                GraphQLInt,
                resolver=lambda s, a, c, i: s.total_count,
                description="The total number of items across all pages.",
            ),
            "nodes": Field(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(node_type))),
                resolver=lambda s, a, c, i: [e.node for e in s.edges],
                description="The page's items without their edge wrappers.",
            ),
        },
        description=f"A paginated list of {node_type.name} items.",
    )
    _CONNECTION_CACHE[name] = connection
    return connection


__all__ = ["connection_type", "page_info_type"]
