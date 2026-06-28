"""Kinora's public GraphQL API gateway (a self-contained sub-application).

A stable, versioned public API surface over the core Kinora domain — books,
pages, shots, scenes/films, sessions, canon, directing prefs — kept deliberately
separate from the internal REST routes in ``app/api/routes/*``. It ships its own
dependency-free GraphQL engine (lexer/parser/type-system/executor/validator),
API-key auth with scopes + per-key rate limits, persisted queries, depth +
complexity limiting, cursor pagination with dataloader batching, error masking,
a subscription bridge to the §5.6 event stream, an introspection/SDL export, a
generated TypeScript client SDK, and a deprecation/versioning policy.

The mountable ASGI sub-app is :func:`app.graphql.app.create_graphql_app`; the
router that mounts it under ``/graphql`` is :data:`app.graphql.app.router`.

See ``app/graphql/DESIGN.md`` for the full design + roadmap.
"""

from __future__ import annotations

from app.graphql.errors import ErrorCode, GraphQLError
from app.graphql.versioning import API_VERSION

__all__ = ["API_VERSION", "ErrorCode", "GraphQLError"]
