"""Shard keys: the value a sharding strategy maps to a shard.

A *shard key* is the column (or tuple of columns) whose value decides which
shard a row lives on. Kinora's natural shard key is ``book_id`` — a book and all
its derived rows (beats, shots, canon versions, render jobs, episodic memories)
co-locate so the per-book read path (`§8`) never fans out. Some tables shard by
``user_id`` instead (the library shelf, preferences). The framework is agnostic:
a :class:`ShardKey` carries one or more named components and a stable,
deterministic *byte encoding* the strategies hash or compare.

Two design rules make routing correct and reproducible:

1. **Stable encoding.** The same logical key always encodes to the same bytes,
   regardless of Python's hash randomisation, dict ordering, or process. We sort
   multi-component keys by name and join with a unit-separator (the same
   ``\\x1f`` trick :mod:`app.db.hashing` uses) so ``{a:1,b:2}`` and ``{b:2,a:1}``
   are identical and ``{a:"1", b:"2"}`` never collides with ``{a:"12"}``.
2. **No truthiness traps.** ``0``, ``""`` and ``False`` are *valid* key values;
   only ``None`` is rejected (a missing shard key cannot be routed).

Nothing here touches a database; a :class:`ShardKey` is a pure value object.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

#: Unit separator — cannot appear in textual key components, so the boundary
#: between components is unambiguous (mirrors :mod:`app.db.hashing`).
_SEP = "\x1f"

#: The Python types we accept as a single shard-key component. Bytes are kept
#: as-is; everything else is rendered to a canonical text form.
ShardKeyValue = str | int | bytes | bool


def _encode_component(value: ShardKeyValue) -> bytes:
    """Encode one component to canonical bytes.

    ``bool`` is checked before ``int`` (``bool`` *is* an ``int`` in Python) so
    ``True``/``False`` get a distinct, stable rendering rather than ``1``/``0``.
    """
    if isinstance(value, bytes):
        return value
    if isinstance(value, bool):
        return b"\x01" if value else b"\x00"
    if isinstance(value, int):
        return str(value).encode("utf-8")
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError(f"unsupported shard-key component type: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class ShardKey:
    """A normalised, byte-encodable shard key (one or more named components).

    Construct via :meth:`of` (single component) or :meth:`compound` (several).
    The frozen dataclass is hashable so a :class:`ShardKey` can itself key a
    dict/set (the directory strategy relies on this).
    """

    #: Sorted ``(name, value)`` pairs. Stored as a tuple so the dataclass is
    #: hashable and the ordering is canonical.
    components: tuple[tuple[str, ShardKeyValue], ...]

    def __post_init__(self) -> None:
        if not self.components:
            raise ValueError("ShardKey must have at least one component")
        seen: set[str] = set()
        for name, value in self.components:
            if not name:
                raise ValueError("shard-key component name must be non-empty")
            if name in seen:
                raise ValueError(f"duplicate shard-key component: {name!r}")
            if value is None:
                raise ValueError(f"shard-key component {name!r} is None (cannot route)")
            seen.add(name)

    # -- constructors -------------------------------------------------------- #

    @classmethod
    def of(cls, value: ShardKeyValue, *, name: str = "key") -> ShardKey:
        """A single-component key, e.g. ``ShardKey.of(book_id)``."""
        return cls(components=((name, value),))

    @classmethod
    def compound(cls, **components: ShardKeyValue) -> ShardKey:
        """A multi-component key from keyword args, e.g. ``compound(book_id=..., user_id=...)``.

        Components are sorted by name so call-order never affects the encoding.
        """
        items = tuple(sorted(components.items(), key=lambda kv: kv[0]))
        return cls(components=items)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, ShardKeyValue]) -> ShardKey:
        """Build from an arbitrary mapping (sorted by name for stability)."""
        items = tuple(sorted(mapping.items(), key=lambda kv: kv[0]))
        return cls(components=items)

    # -- encoding ------------------------------------------------------------ #

    def encode(self) -> bytes:
        """Canonical, stable byte encoding (the strategies hash/compare this)."""
        parts: list[bytes] = []
        for name, value in self.components:
            parts.append(name.encode("utf-8"))
            parts.append(_encode_component(value))
        return _SEP.encode("utf-8").join(parts)

    def hashed(self, *, algo: str = "sha1") -> int:
        """A deterministic non-negative integer hash of the encoded key.

        Uses a cryptographic digest (not Python's randomised ``hash``) so the
        value is identical across processes and restarts — essential for routing
        to be reproducible. ``algo`` selects the hashlib algorithm.
        """
        digest = hashlib.new(algo, self.encode()).digest()
        return int.from_bytes(digest, "big")

    def hashed_mod(self, modulus: int, *, algo: str = "sha1") -> int:
        """``hashed() % modulus`` — the bucket index for modulo-hash sharding."""
        if modulus <= 0:
            raise ValueError("modulus must be > 0")
        return self.hashed(algo=algo) % modulus

    @property
    def single_value(self) -> ShardKeyValue:
        """The lone component's value (raises for compound keys).

        Range and directory strategies operate on a single ordered/looked-up
        value; they call this and fail loudly on a compound key rather than
        guessing which component to use.
        """
        if len(self.components) != 1:
            raise ValueError(
                "single_value requires a one-component key; "
                f"got {len(self.components)} components"
            )
        return self.components[0][1]

    def as_dict(self) -> dict[str, ShardKeyValue]:
        """A plain dict view (e.g. for logging / the directory map)."""
        return dict(self.components)

    def __str__(self) -> str:
        body = ",".join(f"{n}={v!r}" for n, v in self.components)
        return f"ShardKey({body})"


def coerce_key(value: ShardKey | ShardKeyValue | Mapping[str, ShardKeyValue]) -> ShardKey:
    """Coerce a raw value / mapping into a :class:`ShardKey`.

    Lets callers pass a bare ``book_id`` string (the common case) or a mapping
    and get a normalised key, while a :class:`ShardKey` passes through untouched.
    """
    if isinstance(value, ShardKey):
        return value
    if isinstance(value, Mapping):
        return ShardKey.from_mapping(value)
    if isinstance(value, (str, int, bytes, bool)):
        return ShardKey.of(value)
    raise TypeError(f"cannot coerce {type(value).__name__} into a ShardKey")


__all__ = [
    "ShardKey",
    "ShardKeyValue",
    "coerce_key",
]
