"""The GraphQL type system, built in code (no SDL parsing).

Defines the runtime type objects the schema is assembled from — scalars, enums,
objects, interfaces, unions, input objects, and the type *wrappers* ``List`` and
``NonNull``. Resolvers are plain ``async`` (or sync) callables stored on fields;
arguments and input-object fields carry typed coercion via their scalar/enum/
input ``parse_value``. ``coerce_input`` turns an already-decoded literal/JSON
value into the internal Python value the resolver receives (or raises a
:class:`~app.graphql.errors.GraphQLError` with ``BAD_USER_INPUT``).

This module deliberately knows nothing about Kinora's domain; the concrete types
live in ``app/graphql/types/`` and are wired into a :class:`~app.graphql.schema.Schema`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.graphql.errors import GraphQLError, bad_input

# A resolver receives (source, args, context, info) and returns the field value
# (sync or awaitable). ``info`` carries the field node + path for diagnostics.
Resolver = Callable[..., Any]

# Sentinel for "no value supplied" (distinct from an explicit ``null``).
UNDEFINED: Any = object()


class GraphQLType:
    """Base for every type node."""

    name: str = ""

    def unwrap(self) -> GraphQLNamedType:
        """Return the underlying named type, stripping List/NonNull wrappers."""
        node: GraphQLType = self
        while isinstance(node, (GraphQLList, GraphQLNonNull)):
            node = node.of_type
        assert isinstance(node, GraphQLNamedType)
        return node

    def __str__(self) -> str:
        return type_ref_str(self)


class GraphQLNamedType(GraphQLType):
    """A named (non-wrapping) type: scalar, enum, object, interface, union, input."""

    description: str | None = None


class GraphQLWrappingType(GraphQLType):
    of_type: GraphQLType


class GraphQLList(GraphQLWrappingType):
    """A ``[T]`` list type."""

    def __init__(self, of_type: GraphQLType) -> None:
        self.of_type = of_type


class GraphQLNonNull(GraphQLWrappingType):
    """A ``T!`` non-null type."""

    def __init__(self, of_type: GraphQLType) -> None:
        if isinstance(of_type, GraphQLNonNull):
            raise TypeError("cannot wrap a NonNull in a NonNull")
        self.of_type = of_type


def type_ref_str(t: GraphQLType) -> str:
    """Render a type as its GraphQL reference string, e.g. ``[Book!]!``."""
    if isinstance(t, GraphQLNonNull):
        return f"{type_ref_str(t.of_type)}!"
    if isinstance(t, GraphQLList):
        return f"[{type_ref_str(t.of_type)}]"
    return t.name


# --------------------------------------------------------------------------- #
# Scalars + enums
# --------------------------------------------------------------------------- #


class GraphQLScalar(GraphQLNamedType):
    """A leaf scalar with serialize (output) + parse (input) coercion."""

    def __init__(
        self,
        name: str,
        *,
        serialize: Callable[[Any], Any],
        parse_value: Callable[[Any], Any],
        description: str | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self._serialize = serialize
        self._parse_value = parse_value

    def serialize(self, value: Any) -> Any:
        return self._serialize(value)

    def parse_value(self, value: Any) -> Any:
        return self._parse_value(value)


@dataclass(frozen=True, slots=True)
class EnumValue:
    """One enum member: its public name, the internal value, and deprecation."""

    name: str
    value: Any
    description: str | None = None
    deprecation_reason: str | None = None


class GraphQLEnum(GraphQLNamedType):
    """An enum mapping public names to internal Python values."""

    def __init__(
        self,
        name: str,
        values: Sequence[EnumValue | str],
        *,
        description: str | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.values: list[EnumValue] = [
            v if isinstance(v, EnumValue) else EnumValue(v, v) for v in values
        ]
        self._by_name = {v.name: v for v in self.values}
        self._by_value = {v.value: v for v in self.values}

    def serialize(self, value: Any) -> str:
        member = self._by_value.get(value)
        if member is None and value in self._by_name:
            member = self._by_name[value]
        if member is None:
            raise GraphQLError(f"Enum {self.name!r} cannot represent value {value!r}.")
        return member.name

    def parse_value(self, value: Any) -> Any:
        if not isinstance(value, str) or value not in self._by_name:
            raise bad_input(
                f"Expected a value of enum {self.name!r}, got {value!r}.",
            )
        return self._by_name[value].value


# --------------------------------------------------------------------------- #
# Fields, arguments, input fields
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Argument:
    """A field/directive argument with a type, optional default, and description."""

    type: GraphQLType
    default_value: Any = UNDEFINED
    description: str | None = None
    deprecation_reason: str | None = None


@dataclass(slots=True)
class Field:
    """An object/interface field: its type, args, resolver, cost, and deprecation.

    ``cost`` is the gateway's static complexity weight (see ``validate.py``);
    ``list_cost_multiplier=True`` multiplies child cost by the effective ``first``
    page size so a paginated field's cost scales with how much it can return.
    """

    type: GraphQLType
    args: dict[str, Argument] = field(default_factory=dict)
    resolver: Resolver | None = None
    description: str | None = None
    deprecation_reason: str | None = None
    cost: int = 1
    list_cost_multiplier: bool = False
    # The scope an API key must hold to select this field (None => any key).
    required_scope: str | None = None


@dataclass(slots=True)
class InputField:
    """A field of an input object."""

    type: GraphQLType
    default_value: Any = UNDEFINED
    description: str | None = None


# --------------------------------------------------------------------------- #
# Composite types
# --------------------------------------------------------------------------- #


class GraphQLObject(GraphQLNamedType):
    """An output object type with named fields and optional interfaces."""

    def __init__(
        self,
        name: str,
        fields: Callable[[], dict[str, Field]] | dict[str, Field],
        *,
        interfaces: Sequence[GraphQLInterface] = (),
        description: str | None = None,
        is_type_of: Callable[[Any], bool] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.interfaces = list(interfaces)
        self.is_type_of = is_type_of
        self._fields_thunk = fields
        self._fields: dict[str, Field] | None = None

    @property
    def fields(self) -> dict[str, Field]:
        if self._fields is None:
            self._fields = (
                self._fields_thunk()
                if callable(self._fields_thunk)
                else self._fields_thunk
            )
        return self._fields


class GraphQLInterface(GraphQLNamedType):
    """An interface type. ``resolve_type`` maps a value to its concrete object."""

    def __init__(
        self,
        name: str,
        fields: Callable[[], dict[str, Field]] | dict[str, Field],
        *,
        resolve_type: Callable[[Any], str] | None = None,
        description: str | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.resolve_type = resolve_type
        self._fields_thunk = fields
        self._fields: dict[str, Field] | None = None

    @property
    def fields(self) -> dict[str, Field]:
        if self._fields is None:
            self._fields = (
                self._fields_thunk()
                if callable(self._fields_thunk)
                else self._fields_thunk
            )
        return self._fields


class GraphQLUnion(GraphQLNamedType):
    """A union of object types. ``resolve_type`` maps a value to a member name."""

    def __init__(
        self,
        name: str,
        types: Sequence[GraphQLObject],
        *,
        resolve_type: Callable[[Any], str] | None = None,
        description: str | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.types = list(types)
        self.resolve_type = resolve_type


class GraphQLInputObject(GraphQLNamedType):
    """An input object type used for mutation arguments."""

    def __init__(
        self,
        name: str,
        fields: Callable[[], dict[str, InputField]] | dict[str, InputField],
        *,
        description: str | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self._fields_thunk = fields
        self._fields: dict[str, InputField] | None = None

    @property
    def fields(self) -> dict[str, InputField]:
        if self._fields is None:
            self._fields = (
                self._fields_thunk()
                if callable(self._fields_thunk)
                else self._fields_thunk
            )
        return self._fields


# --------------------------------------------------------------------------- #
# Input coercion (decoded value -> internal value, against a type)
# --------------------------------------------------------------------------- #


def coerce_input(type_: GraphQLType, value: Any, *, path: str = "") -> Any:
    """Coerce an already-decoded input ``value`` against ``type_``.

    ``value is UNDEFINED`` means the input was absent (caller applies defaults);
    an explicit ``None`` is a real null and is rejected for a NonNull type.
    Raises :class:`~app.graphql.errors.GraphQLError` (``BAD_USER_INPUT``) with the
    failing input path on mismatch.
    """
    where = f" at {path}" if path else ""
    if isinstance(type_, GraphQLNonNull):
        if value is None:
            raise bad_input(f"Expected a non-null value{where}.")
        if value is UNDEFINED:
            raise bad_input(f"Missing required value{where}.")
        return coerce_input(type_.of_type, value, path=path)
    if value is None or value is UNDEFINED:
        return None
    if isinstance(type_, GraphQLList):
        items = value if isinstance(value, (list, tuple)) else [value]
        return [
            coerce_input(type_.of_type, item, path=f"{path}[{i}]")
            for i, item in enumerate(items)
        ]
    if isinstance(type_, (GraphQLScalar, GraphQLEnum)):
        try:
            return type_.parse_value(value)
        except GraphQLError as exc:
            raise bad_input(f"{exc.message}{where}") from exc
    if isinstance(type_, GraphQLInputObject):
        return _coerce_input_object(type_, value, path)
    raise bad_input(f"Type {type_!s} is not a valid input type{where}.")


def _coerce_input_object(type_: GraphQLInputObject, value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise bad_input(f"Expected an object for {type_.name}{_at(path)}.")
    unknown = set(value) - set(type_.fields)
    if unknown:
        raise bad_input(
            f"Unknown field(s) {sorted(unknown)!r} on input {type_.name}{_at(path)}."
        )
    out: dict[str, Any] = {}
    for fname, fdef in type_.fields.items():
        child_path = f"{path}.{fname}" if path else fname
        if fname in value:
            out[fname] = coerce_input(fdef.type, value[fname], path=child_path)
        elif fdef.default_value is not UNDEFINED:
            out[fname] = fdef.default_value
        elif isinstance(fdef.type, GraphQLNonNull):
            raise bad_input(f"Missing required field {fname!r} on {type_.name}{_at(path)}.")
        else:
            out[fname] = None
    return out


def _at(path: str) -> str:
    return f" at {path}" if path else ""


# Resolver type alias re-exported for resolvers/ modules.
AsyncResolver = Callable[..., Awaitable[Any]]

__all__ = [
    "UNDEFINED",
    "Argument",
    "AsyncResolver",
    "EnumValue",
    "Field",
    "GraphQLEnum",
    "GraphQLInputObject",
    "GraphQLInterface",
    "GraphQLList",
    "GraphQLNamedType",
    "GraphQLNonNull",
    "GraphQLObject",
    "GraphQLScalar",
    "GraphQLType",
    "GraphQLUnion",
    "GraphQLWrappingType",
    "InputField",
    "Resolver",
    "coerce_input",
    "type_ref_str",
]
