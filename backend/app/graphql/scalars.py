"""Built-in + custom scalar types for the gateway schema.

The five spec scalars (``Int``, ``Float``, ``String``, ``Boolean``, ``ID``) plus
the gateway's custom scalars:

* ``DateTime`` — an ISO-8601 string (serializes ``datetime`` or passes through a
  string); the domain stores timestamps as ISO strings already.
* ``JSON`` — an arbitrary JSON value, for the domain's free-form blobs
  (``source_span``, ``qa``, ``word_boxes``, sync segments). Serialized as-is.
* ``Cursor`` — an opaque pagination cursor (a base64 string); see
  ``app/graphql/pagination.py``.

Coercion is strict on input (a string is *not* silently accepted for ``Int``)
but lenient enough to be ergonomic (an int is accepted for ``Float``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.graphql.errors import GraphQLError
from app.graphql.type_system import GraphQLScalar

# 32-bit signed range, per the GraphQL Int spec.
_INT_MIN = -(2**31)
_INT_MAX = 2**31 - 1


def _serialize_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise GraphQLError(f"Int cannot represent value {value!r}.")


def _parse_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GraphQLError(f"Int cannot represent non-integer value {value!r}.")
    if not _INT_MIN <= value <= _INT_MAX:
        raise GraphQLError(f"Int cannot represent value outside 32-bit range: {value!r}.")
    return value


def _serialize_float(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    raise GraphQLError(f"Float cannot represent value {value!r}.")


def _parse_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GraphQLError(f"Float cannot represent non-numeric value {value!r}.")
    return float(value)


def _serialize_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    raise GraphQLError(f"String cannot represent value {value!r}.")


def _parse_string(value: Any) -> str:
    if not isinstance(value, str):
        raise GraphQLError(f"String cannot represent a non-string value {value!r}.")
    return value


def _serialize_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise GraphQLError(f"Boolean cannot represent value {value!r}.")


def _parse_boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise GraphQLError(f"Boolean cannot represent non-boolean value {value!r}.")
    return value


def _serialize_id(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    raise GraphQLError(f"ID cannot represent value {value!r}.")


def _parse_id(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    raise GraphQLError(f"ID cannot represent value {value!r}.")


def _serialize_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    raise GraphQLError(f"DateTime cannot represent value {value!r}.")


def _parse_datetime(value: Any) -> str:
    if not isinstance(value, str):
        raise GraphQLError("DateTime must be an ISO-8601 string.")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GraphQLError(f"DateTime is not a valid ISO-8601 string: {value!r}.") from exc
    return value


def _passthrough(value: Any) -> Any:
    return value


def _parse_cursor(value: Any) -> str:
    if not isinstance(value, str):
        raise GraphQLError("Cursor must be a string.")
    return value


GraphQLInt = GraphQLScalar(
    "Int",
    serialize=_serialize_int,
    parse_value=_parse_int,
    description="A signed 32-bit integer.",
)
GraphQLFloat = GraphQLScalar(
    "Float",
    serialize=_serialize_float,
    parse_value=_parse_float,
    description="A double-precision floating-point value.",
)
GraphQLString = GraphQLScalar(
    "String",
    serialize=_serialize_string,
    parse_value=_parse_string,
    description="A UTF-8 character sequence.",
)
GraphQLBoolean = GraphQLScalar(
    "Boolean",
    serialize=_serialize_boolean,
    parse_value=_parse_boolean,
    description="A true or false value.",
)
GraphQLID = GraphQLScalar(
    "ID",
    serialize=_serialize_id,
    parse_value=_parse_id,
    description="A unique identifier, serialized as a String.",
)
GraphQLDateTime = GraphQLScalar(
    "DateTime",
    serialize=_serialize_datetime,
    parse_value=_parse_datetime,
    description="An ISO-8601 encoded UTC date-time string.",
)
GraphQLJSON = GraphQLScalar(
    "JSON",
    serialize=_passthrough,
    parse_value=_passthrough,
    description="An arbitrary JSON value (object, array, or scalar).",
)
GraphQLCursor = GraphQLScalar(
    "Cursor",
    serialize=_passthrough,
    parse_value=_parse_cursor,
    description="An opaque cursor for Relay-style pagination.",
)

#: Every built-in/custom scalar, keyed by name (for the schema type registry).
BUILTIN_SCALARS = {
    s.name: s
    for s in (
        GraphQLInt,
        GraphQLFloat,
        GraphQLString,
        GraphQLBoolean,
        GraphQLID,
        GraphQLDateTime,
        GraphQLJSON,
        GraphQLCursor,
    )
}

__all__ = [
    "BUILTIN_SCALARS",
    "GraphQLBoolean",
    "GraphQLCursor",
    "GraphQLDateTime",
    "GraphQLFloat",
    "GraphQLID",
    "GraphQLInt",
    "GraphQLJSON",
    "GraphQLString",
]
