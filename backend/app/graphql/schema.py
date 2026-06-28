"""The :class:`Schema` — a built-in-code type registry + the operation roots.

A schema bundles the query/mutation/subscription root objects and a *closure* of
every named type reachable from them (collected by walking fields, arguments,
input objects, interfaces, and unions). The collected registry powers
introspection, the SDL printer, the ``node`` resolver, and validation.

The schema is assembled once at import time (``app/graphql/root.py``) and reused
across requests; it holds no per-request state.
"""

from __future__ import annotations

from app.graphql.scalars import BUILTIN_SCALARS
from app.graphql.type_system import (
    GraphQLEnum,
    GraphQLInputObject,
    GraphQLInterface,
    GraphQLList,
    GraphQLNamedType,
    GraphQLNonNull,
    GraphQLObject,
    GraphQLScalar,
    GraphQLType,
    GraphQLUnion,
)


class SchemaError(Exception):
    """Raised when a schema is internally inconsistent (a build-time bug)."""


class Schema:
    """A runnable GraphQL schema: roots + the reachable named-type registry."""

    def __init__(
        self,
        *,
        query: GraphQLObject,
        mutation: GraphQLObject | None = None,
        subscription: GraphQLObject | None = None,
        extra_types: list[GraphQLNamedType] | None = None,
    ) -> None:
        self.query = query
        self.mutation = mutation
        self.subscription = subscription
        self.type_map: dict[str, GraphQLNamedType] = {}
        # Seed the built-in scalars so they always exist in the registry.
        for scalar in BUILTIN_SCALARS.values():
            self.type_map[scalar.name] = scalar
        roots: list[GraphQLNamedType] = [query]
        if mutation is not None:
            roots.append(mutation)
        if subscription is not None:
            roots.append(subscription)
        roots.extend(extra_types or [])
        for root in roots:
            self._collect(root)
        self._index_implementations()

    # -- registry construction ---------------------------------------------- #

    def _collect(self, type_: GraphQLType) -> None:
        named = type_.unwrap() if isinstance(type_, (GraphQLList, GraphQLNonNull)) else type_
        if not isinstance(named, GraphQLNamedType):
            return
        existing = self.type_map.get(named.name)
        if existing is not None:
            if existing is not named:
                raise SchemaError(f"duplicate type name {named.name!r}")
            return
        self.type_map[named.name] = named
        if isinstance(named, (GraphQLObject, GraphQLInterface)):
            for fld in named.fields.values():
                self._collect(fld.type)
                for arg in fld.args.values():
                    self._collect(arg.type)
            if isinstance(named, GraphQLObject):
                for iface in named.interfaces:
                    self._collect(iface)
        elif isinstance(named, GraphQLInputObject):
            for ifld in named.fields.values():
                self._collect(ifld.type)
        elif isinstance(named, GraphQLUnion):
            for member in named.types:
                self._collect(member)

    def _index_implementations(self) -> None:
        """Build interface → implementing-objects, validating field coverage."""
        self.implementations: dict[str, list[GraphQLObject]] = {}
        for named in self.type_map.values():
            if isinstance(named, GraphQLObject):
                for iface in named.interfaces:
                    self.implementations.setdefault(iface.name, []).append(named)
                    missing = set(iface.fields) - set(named.fields)
                    if missing:
                        raise SchemaError(
                            f"{named.name} claims {iface.name} but is missing {sorted(missing)}"
                        )

    # -- lookups ------------------------------------------------------------- #

    def get_type(self, name: str) -> GraphQLNamedType | None:
        return self.type_map.get(name)

    def root_for(self, operation: str) -> GraphQLObject | None:
        if operation == "query":
            return self.query
        if operation == "mutation":
            return self.mutation
        if operation == "subscription":
            return self.subscription
        return None

    def is_possible_type(self, abstract_name: str, object_name: str) -> bool:
        """Whether ``object_name`` is a possible concrete type of an interface/union."""
        abstract = self.type_map.get(abstract_name)
        if isinstance(abstract, GraphQLUnion):
            return any(t.name == object_name for t in abstract.types)
        if isinstance(abstract, GraphQLInterface):
            return any(
                obj.name == object_name for obj in self.implementations.get(abstract_name, [])
            )
        return abstract_name == object_name

    def named_types(self) -> list[GraphQLNamedType]:
        """All registered named types in a stable (sorted-by-name) order."""
        return [self.type_map[name] for name in sorted(self.type_map)]

    def scalars(self) -> list[GraphQLScalar]:
        return [t for t in self.named_types() if isinstance(t, GraphQLScalar)]

    def enums(self) -> list[GraphQLEnum]:
        return [t for t in self.named_types() if isinstance(t, GraphQLEnum)]

    def objects(self) -> list[GraphQLObject]:
        return [t for t in self.named_types() if isinstance(t, GraphQLObject)]


__all__ = ["Schema", "SchemaError"]
