"""The global ``Node`` interface and opaque global-id encoding.

Relay's ``Node`` pattern gives every persistent object a globally-unique opaque
``id`` and a single ``Query.node(id:)`` entry point to refetch any object. The
global id is ``base64("<TypeName>:<localId>")`` so the ``node`` resolver can
decode the type and dispatch to the right loader. The interface's
``resolve_type`` maps a loaded value to its concrete object type via a
``__typename`` attribute set by the loaders, or by the row's class name.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from app.graphql.errors import bad_input
from app.graphql.scalars import GraphQLID
from app.graphql.type_system import Field, GraphQLInterface, GraphQLNonNull


def global_id(type_name: str, local_id: str) -> str:
    """Encode a ``(type, localId)`` pair into an opaque global id."""
    return base64.urlsafe_b64encode(f"{type_name}:{local_id}".encode()).decode("ascii")


def from_global_id(gid: str) -> tuple[str, str]:
    """Decode a global id back to ``(type_name, local_id)`` (raises ``BAD_USER_INPUT``)."""
    try:
        raw = base64.urlsafe_b64decode(gid.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise bad_input(f"Invalid node id {gid!r}.") from exc
    if ":" not in raw:
        raise bad_input(f"Invalid node id {gid!r}.")
    type_name, local_id = raw.split(":", 1)
    if not type_name or not local_id:
        raise bad_input(f"Invalid node id {gid!r}.")
    return type_name, local_id


# Maps a loaded row to its concrete GraphQL type name. Loaders stamp ``__typename``
# on projection dicts; ORM rows fall back to their class name.
_CLASS_TO_TYPE = {
    "Book": "Book",
    "Shot": "Shot",
    "Scene": "Scene",
    "Page": "Page",
    "Session": "Session",
}


def _resolve_node_type(value: Any) -> str:
    if isinstance(value, dict) and "__typename" in value:
        return str(value["__typename"])
    stamped = getattr(value, "__typename", None)
    if isinstance(stamped, str):
        return stamped
    cls_name = type(value).__name__
    return _CLASS_TO_TYPE.get(cls_name, cls_name)


NODE_INTERFACE = GraphQLInterface(
    "Node",
    lambda: {
        "id": Field(
            GraphQLNonNull(GraphQLID),
            description="An opaque, globally-unique identifier.",
        )
    },
    resolve_type=_resolve_node_type,
    description="An object with a globally-unique id, refetchable via `node`.",
)


__all__ = ["NODE_INTERFACE", "from_global_id", "global_id"]
