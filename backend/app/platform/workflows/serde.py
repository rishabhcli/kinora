"""Payload serialisation for durable workflow state.

Workflow arguments, activity inputs/outputs, signal payloads and results all
cross the durable boundary (they are persisted in the event history and reloaded
on replay). They must therefore round-trip through JSON losslessly *and*
deterministically — the same value must serialise to the same bytes every time,
or replay would diverge.

This module provides two small, dependency-free converters:

* :func:`to_jsonable` — turn arbitrary Python values into JSON-native ones,
  with explicit support for :class:`datetime`/:class:`date`, :class:`Decimal`,
  :class:`enum.Enum`, sets (as sorted lists), tuples (as lists), and dataclasses
  (as their field dict). Mappings are emitted with **sorted keys** so the JSON is
  canonical.
* :func:`from_jsonable` — the identity-ish inverse for the JSON-native subset
  (datetimes are *not* auto-revived, because the type is erased in JSON; activity
  results that need rich types should use plain dicts, which is what the engine's
  own attributes always do).

The goal is determinism and durability, not a full object graph serialiser:
workflow/activity payloads should be JSON-shaped data (dicts/lists/scalars),
which is also what keeps them debuggable in the stored history.
"""

from __future__ import annotations

import dataclasses
import enum
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def to_jsonable(value: Any) -> Any:
    """Recursively convert ``value`` to JSON-native types, canonically.

    Mappings get sorted keys; sets become sorted lists; tuples become lists;
    dataclass instances become their field dicts; enums become their value;
    datetimes/dates become ISO strings; Decimals become strings (lossless).
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, enum.Enum):
        return to_jsonable(value.value)
    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if isinstance(value, Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": value.hex()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: to_jsonable(v) for k, v in sorted(dataclasses.asdict(value).items())}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((to_jsonable(v) for v in value), key=_sort_key)
    raise TypeError(f"value of type {type(value).__name__!r} is not workflow-serialisable")


def from_jsonable(value: Any) -> Any:
    """Inverse of :func:`to_jsonable` for the tagged rich types it emits."""
    if isinstance(value, dict):
        if "__dt__" in value and len(value) == 1:
            return datetime.fromisoformat(value["__dt__"])
        if "__date__" in value and len(value) == 1:
            return date.fromisoformat(value["__date__"])
        if "__decimal__" in value and len(value) == 1:
            return Decimal(value["__decimal__"])
        if "__bytes__" in value and len(value) == 1:
            return bytes.fromhex(value["__bytes__"])
        return {k: from_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_jsonable(v) for v in value]
    return value


def dumps(value: Any) -> str:
    """Canonical JSON string of ``value`` (sorted keys, compact separators)."""
    return json.dumps(to_jsonable(value), sort_keys=True, separators=(",", ":"))


def loads(text: str) -> Any:
    """Parse a canonical JSON string back into Python values."""
    return from_jsonable(json.loads(text))


def _sort_key(value: Any) -> str:
    """A total-order key for serialised set members (stringified canonical)."""
    return json.dumps(value, sort_keys=True)


__all__ = ["dumps", "from_jsonable", "loads", "to_jsonable"]
