"""Meta types: ``Viewer`` (the API key's owner), ``ApiVersion``, and deprecations.

``Viewer`` is the public account behind the presenting API key — its id, the
key's label, and its granted scopes — so a client can confirm *who* and *with
what access* it is acting as. ``ApiVersion`` surfaces the published contract
version, stability, and live deprecation list (``app/graphql/versioning.py``).
"""

from __future__ import annotations

from typing import Any

from app.graphql.scalars import GraphQLID, GraphQLString
from app.graphql.type_system import (
    Field,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObject,
)

_CACHE: dict[str, GraphQLObject] = {}


def deprecation_type() -> GraphQLObject:
    if "Deprecation" not in _CACHE:
        _CACHE["Deprecation"] = GraphQLObject(
            "Deprecation",
            {
                "coordinate": Field(
                    GraphQLNonNull(GraphQLString),
                    resolver=lambda s, a, c, i: s["coordinate"],
                    description="The schema member (e.g. `Book.legacyId`).",
                ),
                "reason": Field(
                    GraphQLNonNull(GraphQLString), resolver=lambda s, a, c, i: s["reason"]
                ),
                "since": Field(
                    GraphQLNonNull(GraphQLString), resolver=lambda s, a, c, i: s["since"]
                ),
                "plannedRemoval": Field(
                    GraphQLString, resolver=lambda s, a, c, i: s.get("plannedRemoval")
                ),
            },
            description="A deprecated schema member + its lifecycle metadata.",
        )
    return _CACHE["Deprecation"]


def api_version_type() -> GraphQLObject:
    if "ApiVersion" not in _CACHE:
        _CACHE["ApiVersion"] = GraphQLObject(
            "ApiVersion",
            lambda: {
                "version": Field(
                    GraphQLNonNull(GraphQLString),
                    resolver=lambda s, a, c, i: s["version"],
                    description="Semantic version of the public GraphQL contract.",
                ),
                "stability": Field(
                    GraphQLNonNull(GraphQLString),
                    resolver=lambda s, a, c, i: s["stability"],
                ),
                "deprecationWindow": Field(
                    GraphQLNonNull(GraphQLString),
                    resolver=lambda s, a, c, i: s["deprecationWindow"],
                ),
                "deprecations": Field(
                    GraphQLNonNull(GraphQLList(GraphQLNonNull(deprecation_type()))),
                    resolver=lambda s, a, c, i: s["deprecations"],
                ),
            },
            description="The live API version, stability, and deprecation policy.",
        )
    return _CACHE["ApiVersion"]


def viewer_type() -> GraphQLObject:
    if "Viewer" not in _CACHE:
        _CACHE["Viewer"] = GraphQLObject(
            "Viewer",
            {
                "userId": Field(
                    GraphQLNonNull(GraphQLID),
                    resolver=lambda s, a, c, i: s["userId"],
                    description="The account that owns the presenting API key.",
                ),
                "keyLabel": Field(
                    GraphQLString, resolver=lambda s, a, c, i: s.get("keyLabel")
                ),
                "scopes": Field(
                    GraphQLNonNull(GraphQLList(GraphQLNonNull(GraphQLString))),
                    resolver=lambda s, a, c, i: s.get("scopes", []),
                    description="The scopes granted to the presenting API key.",
                ),
            },
            description="The authenticated principal behind the presenting API key.",
        )
    return _CACHE["Viewer"]


def resolve_viewer(source: Any, args: dict[str, Any], ctx: Any, info: Any) -> dict[str, Any]:
    return {
        "userId": ctx.api_key.user_id,
        "keyLabel": ctx.api_key.label,
        "scopes": list(ctx.api_key.scopes),
    }


__all__ = [
    "api_version_type",
    "deprecation_type",
    "resolve_viewer",
    "viewer_type",
]
