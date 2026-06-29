"""Value codecs: how a Python field value becomes the bytes that get encrypted.

Encryption operates on bytes; ORM columns hold ``str``/``int``/``dict``/etc. A
:class:`Codec` is the bijection between the two. Keeping this separate from the
encryption mechanism means a single :class:`EncryptedType` can transparently
carry a string, JSON, or an int simply by choosing a codec, and the round-trip
type is preserved (``decode(encode(x)) == x``).

Codecs are *self-describing where it matters*: the JSON codec emits canonical,
sorted-key UTF-8 so that a value used for deterministic encryption / blind
indexing hashes identically regardless of dict insertion order.
"""

from __future__ import annotations

import json
from typing import Any, Generic, Protocol, TypeVar

T = TypeVar("T")


class Codec(Protocol[T]):
    """Encode a typed value to bytes and back (an exact round-trip)."""

    def encode(self, value: T) -> bytes: ...

    def decode(self, raw: bytes) -> T: ...


class StringCodec:
    """UTF-8 text codec (the default for encrypted ``str`` columns)."""

    def encode(self, value: str) -> bytes:
        return value.encode("utf-8")

    def decode(self, raw: bytes) -> str:
        return raw.decode("utf-8")


class BytesCodec:
    """Pass-through codec for raw ``bytes`` columns."""

    def encode(self, value: bytes) -> bytes:
        return value

    def decode(self, raw: bytes) -> bytes:
        return raw


class IntCodec:
    """Decimal-text codec for integers (range-query friendly via blind buckets)."""

    def encode(self, value: int) -> bytes:
        return str(int(value)).encode("ascii")

    def decode(self, raw: bytes) -> int:
        return int(raw.decode("ascii"))


class JsonCodec(Generic[T]):
    """Canonical-JSON codec (sorted keys, compact separators) for structured data.

    Canonical encoding is what makes a JSON value safe to deterministically
    encrypt or blind-index: ``{"a":1,"b":2}`` and ``{"b":2,"a":1}`` hash the same.
    """

    def encode(self, value: T) -> bytes:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    def decode(self, raw: bytes) -> T:
        value: T = json.loads(raw.decode("utf-8"))
        return value


#: Conventional shared instances (stateless, so safe to reuse).
STRING = StringCodec()
BYTES = BytesCodec()
INT = IntCodec()
JSON: JsonCodec[Any] = JsonCodec()


__all__ = [
    "BYTES",
    "INT",
    "JSON",
    "STRING",
    "BytesCodec",
    "Codec",
    "IntCodec",
    "JsonCodec",
    "StringCodec",
]
