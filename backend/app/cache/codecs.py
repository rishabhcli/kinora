"""Serialization codecs — turn cached values into bytes and back.

The L1 (in-process) backend stores live Python objects and needs no codec; the
L2 (Redis) backend stores ``bytes``. A :class:`Codec` is the seam between them.
Codecs are intentionally pluggable per-namespace: a namespace holding plain
JSON-able dicts uses :class:`JsonCodec` (portable, debuggable in ``redis-cli``);
a namespace holding arbitrary Python objects uses :class:`PickleCodec`.

All codecs round-trip through ``bytes`` and raise
:class:`~app.cache.errors.SerializationError` on failure so a single corrupt
payload never escapes as a raw ``pickle``/``json`` exception.

Negative-cache sentinels and tombstones are encoded by wrapping the codec in an
envelope at the :class:`~app.cache.entry.CacheEntry` layer, not here — codecs
only concern themselves with the user value.
"""

from __future__ import annotations

import json
import pickle
from typing import Any, Protocol, runtime_checkable

from app.cache.errors import SerializationError


@runtime_checkable
class Codec(Protocol):
    """Bidirectional value <-> bytes serializer."""

    #: Short stable name, embedded in metrics/labels; must be unique per codec.
    name: str

    def encode(self, value: Any) -> bytes:
        """Serialize ``value`` to bytes (raises :class:`SerializationError`)."""
        ...

    def decode(self, raw: bytes) -> Any:
        """Deserialize ``raw`` bytes back to a value (raises :class:`SerializationError`)."""
        ...


class JsonCodec:
    """UTF-8 JSON codec — portable and human-readable in ``redis-cli``.

    Use for values that are already JSON-able (dicts/lists/primitives). Compact
    separators keep payloads small; non-ASCII is preserved (``ensure_ascii`` off).
    """

    name = "json"

    __slots__ = ("_default", "_sort_keys")

    def __init__(self, *, sort_keys: bool = False, default: Any = None) -> None:
        self._sort_keys = sort_keys
        self._default = default

    def encode(self, value: Any) -> bytes:
        try:
            text = json.dumps(
                value,
                separators=(",", ":"),
                ensure_ascii=False,
                sort_keys=self._sort_keys,
                default=self._default,
            )
        except (TypeError, ValueError) as exc:
            raise SerializationError(f"json encode failed: {exc}") from exc
        return text.encode("utf-8")

    def decode(self, raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SerializationError(f"json decode failed: {exc}") from exc


class PickleCodec:
    """Pickle codec — handles arbitrary picklable Python objects.

    Only use for trusted, internally-produced values: never decode a pickle from
    an untrusted source. Defaults to the highest protocol for compactness.
    """

    name = "pickle"

    __slots__ = ("_protocol",)

    def __init__(self, protocol: int = pickle.HIGHEST_PROTOCOL) -> None:
        self._protocol = protocol

    def encode(self, value: Any) -> bytes:
        try:
            return pickle.dumps(value, protocol=self._protocol)
        except (pickle.PicklingError, TypeError, AttributeError) as exc:
            raise SerializationError(f"pickle encode failed: {exc}") from exc

    def decode(self, raw: bytes) -> Any:
        try:
            return pickle.loads(raw)  # noqa: S301 - trusted, internal-only payloads
        except (pickle.UnpicklingError, EOFError, ValueError, TypeError) as exc:
            raise SerializationError(f"pickle decode failed: {exc}") from exc


class BytesCodec:
    """Identity codec for values that are already ``bytes`` (or ``str``).

    ``str`` is UTF-8 encoded on the way in and decoded back to ``str`` on the way
    out only if it was a ``str`` originally — to keep the round-trip stable we
    require the caller to store one type per namespace.
    """

    name = "bytes"

    __slots__ = ("_text",)

    def __init__(self, *, text: bool = False) -> None:
        #: When True, treat values as ``str`` and (de)code UTF-8.
        self._text = text

    def encode(self, value: Any) -> bytes:
        if self._text:
            if not isinstance(value, str):
                raise SerializationError("BytesCodec(text=True) expects str values")
            return value.encode("utf-8")
        if not isinstance(value, bytes | bytearray):
            raise SerializationError("BytesCodec expects bytes values")
        return bytes(value)

    def decode(self, raw: bytes) -> Any:
        if self._text:
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SerializationError(f"utf-8 decode failed: {exc}") from exc
        return raw


#: Default codec used when a namespace does not specify one.
DEFAULT_CODEC: Codec = JsonCodec()


__all__ = [
    "DEFAULT_CODEC",
    "BytesCodec",
    "Codec",
    "JsonCodec",
    "PickleCodec",
]
