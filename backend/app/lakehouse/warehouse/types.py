"""The lakehouse logical type system and the in-memory column vector.

A warehouse column has a **logical type** (the semantic type the schema declares)
that maps onto a small set of physical representations. Values are nullable; a
column vector therefore carries both a Python list of values *and* a null bitmap
(``valid[i] is False`` ⇒ logical NULL, and the slot holds a type-appropriate
placeholder so vectorized kernels never see ``None``).

Everything is pure and deterministic:

* :class:`LogicalType` — the closed enum of supported column types.
* :class:`Field` / :class:`Schema` — named, typed, nullable columns.
* :class:`ColumnVector` — a typed, nullable, in-memory column (the unit the query
  engine and the encoders both speak).

Design notes
------------
* We deliberately keep the physical universe tiny (int64, float64, bool, str,
  bytes, timestamp-as-int64-micros) so the encoders and the vectorized kernels stay
  small and exhaustively testable. ``DECIMAL`` is represented as a scaled int64.
* ``NULL`` semantics follow SQL three-valued logic in the predicate/expression
  layers; the vector itself only records *which* slots are valid.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


class LogicalType(enum.StrEnum):
    """The closed set of column logical types the warehouse supports."""

    BOOL = "bool"
    INT32 = "int32"
    INT64 = "int64"
    FLOAT32 = "float32"
    FLOAT64 = "float64"
    STRING = "string"
    BYTES = "bytes"
    # Microseconds since the Unix epoch, UTC. Stored physically as int64.
    TIMESTAMP = "timestamp"
    # Scaled integer; the scale lives on the Field, not the type.
    DECIMAL = "decimal"

    @property
    def is_integer(self) -> bool:
        return self in (
            LogicalType.INT32,
            LogicalType.INT64,
            LogicalType.TIMESTAMP,
            LogicalType.DECIMAL,
        )

    @property
    def is_floating(self) -> bool:
        return self in (LogicalType.FLOAT32, LogicalType.FLOAT64)

    @property
    def is_numeric(self) -> bool:
        return self.is_integer or self.is_floating

    @property
    def is_ordered(self) -> bool:
        """Whether ``<`` / ``>`` comparisons (and thus min/max stats) are defined."""
        return self in (
            LogicalType.INT32,
            LogicalType.INT64,
            LogicalType.FLOAT32,
            LogicalType.FLOAT64,
            LogicalType.STRING,
            LogicalType.BYTES,
            LogicalType.TIMESTAMP,
            LogicalType.DECIMAL,
            LogicalType.BOOL,
        )


# The placeholder stored in a vector slot whose validity bit is False. Chosen to be
# the natural "zero" of the physical representation so numeric kernels can ignore the
# bitmap on a fast path without producing garbage.
_PLACEHOLDER: dict[LogicalType, Any] = {
    LogicalType.BOOL: False,
    LogicalType.INT32: 0,
    LogicalType.INT64: 0,
    LogicalType.FLOAT32: 0.0,
    LogicalType.FLOAT64: 0.0,
    LogicalType.STRING: "",
    LogicalType.BYTES: b"",
    LogicalType.TIMESTAMP: 0,
    LogicalType.DECIMAL: 0,
}


def placeholder_for(dtype: LogicalType) -> Any:
    """The non-null placeholder a vector stores in invalid slots of ``dtype``."""
    return _PLACEHOLDER[dtype]


def _coerce(dtype: LogicalType, value: Any) -> Any:
    """Coerce a Python value to the canonical physical representation of ``dtype``.

    Raises :class:`TypeError` / :class:`ValueError` on an incompatible value so that
    bad data fails loud at ingest rather than corrupting a column.
    """
    if dtype is LogicalType.BOOL:
        if isinstance(value, bool):
            return value
        raise TypeError(f"expected bool, got {type(value).__name__}")
    if dtype in (LogicalType.INT32, LogicalType.INT64, LogicalType.DECIMAL):
        if isinstance(value, bool):
            raise TypeError("bool is not a valid integer value")
        if isinstance(value, int):
            return value
        raise TypeError(f"expected int for {dtype}, got {type(value).__name__}")
    if dtype in (LogicalType.FLOAT32, LogicalType.FLOAT64):
        if isinstance(value, bool):
            raise TypeError("bool is not a valid float value")
        if isinstance(value, int | float):
            return float(value)
        raise TypeError(f"expected float for {dtype}, got {type(value).__name__}")
    if dtype is LogicalType.STRING:
        if isinstance(value, str):
            return value
        raise TypeError(f"expected str, got {type(value).__name__}")
    if dtype is LogicalType.BYTES:
        if isinstance(value, bytes | bytearray):
            return bytes(value)
        raise TypeError(f"expected bytes, got {type(value).__name__}")
    # TIMESTAMP — accept aware/naive datetime (naive assumed UTC) or raw int micros.
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return int(aware.astimezone(UTC).timestamp() * 1_000_000)
    if isinstance(value, bool):
        raise TypeError("bool is not a valid timestamp value")
    if isinstance(value, int):
        return value
    raise TypeError(f"expected datetime or int micros, got {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class Field:
    """A named, typed, nullable column declaration.

    ``scale`` only applies to ``DECIMAL`` (number of fractional digits encoded into
    the scaled int64). ``metadata`` is opaque to the warehouse and carried through
    untouched (sibling facets stash semantic-layer hints there).
    """

    name: str
    dtype: LogicalType
    nullable: bool = True
    scale: int = 0
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("field name must be non-empty")
        if self.scale and self.dtype is not LogicalType.DECIMAL:
            raise ValueError("scale is only valid for DECIMAL fields")
        if self.scale < 0:
            raise ValueError("scale must be >= 0")


@dataclass(frozen=True, slots=True)
class Schema:
    """An ordered list of uniquely-named fields."""

    fields: tuple[Field, ...]

    def __post_init__(self) -> None:
        names = [f.name for f in self.fields]
        if len(names) != len(set(names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"duplicate field names: {dupes}")

    @classmethod
    def of(cls, *fields: Field) -> Schema:
        return cls(tuple(fields))

    @property
    def names(self) -> list[str]:
        return [f.name for f in self.fields]

    def field(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)

    def index_of(self, name: str) -> int:
        for i, f in enumerate(self.fields):
            if f.name == name:
                return i
        raise KeyError(name)

    def has(self, name: str) -> bool:
        return any(f.name == name for f in self.fields)

    def select(self, names: list[str]) -> Schema:
        """A projected schema preserving the requested order."""
        return Schema(tuple(self.field(n) for n in names))

    def with_fields(self, extra: list[Field]) -> Schema:
        return Schema(self.fields + tuple(extra))


class ColumnVector:
    """A typed, nullable, in-memory column.

    Invariants (checked in :meth:`__init__`):

    * ``len(values) == len(valid)``.
    * Every valid slot holds a value already coerced to ``dtype``'s physical form.
    * Every invalid slot holds :func:`placeholder_for` ``(dtype)``.

    The vector is the lingua franca between the encoders (which serialise it) and
    the query engine (whose kernels operate on it). It is mutable only via the
    construction helpers below; in-place mutation is avoided so plans stay
    deterministic.
    """

    __slots__ = ("dtype", "_values", "_valid")

    def __init__(self, dtype: LogicalType, values: list[Any], valid: list[bool]) -> None:
        if len(values) != len(valid):
            raise ValueError("values and valid must have equal length")
        self.dtype = dtype
        self._values = values
        self._valid = valid

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_pylist(cls, dtype: LogicalType, items: list[Any]) -> ColumnVector:
        """Build a vector from a Python list where ``None`` marks NULL."""
        ph = placeholder_for(dtype)
        values: list[Any] = []
        valid: list[bool] = []
        for item in items:
            if item is None:
                values.append(ph)
                valid.append(False)
            else:
                values.append(_coerce(dtype, item))
                valid.append(True)
        return cls(dtype, values, valid)

    @classmethod
    def empty(cls, dtype: LogicalType) -> ColumnVector:
        return cls(dtype, [], [])

    # -- accessors ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._values)

    @property
    def values(self) -> list[Any]:
        """The raw physical values (placeholders in invalid slots). Read-only view."""
        return self._values

    @property
    def valid(self) -> list[bool]:
        """The null bitmap (``True`` ⇒ present). Read-only view."""
        return self._valid

    def is_valid(self, i: int) -> bool:
        return self._valid[i]

    def value(self, i: int) -> Any:
        """The physical value at ``i`` regardless of validity (placeholder if null)."""
        return self._values[i]

    def get(self, i: int) -> Any:
        """The logical value at ``i`` (``None`` if null)."""
        return self._values[i] if self._valid[i] else None

    @property
    def null_count(self) -> int:
        return sum(1 for v in self._valid if not v)

    def to_pylist(self) -> list[Any]:
        """A Python list with ``None`` in null slots (the inverse of from_pylist)."""
        return [v if ok else None for v, ok in zip(self._values, self._valid, strict=True)]

    # -- transforms -----------------------------------------------------------

    def take(self, indices: list[int]) -> ColumnVector:
        """A new vector gathering rows at ``indices`` (preserving order)."""
        return ColumnVector(
            self.dtype,
            [self._values[i] for i in indices],
            [self._valid[i] for i in indices],
        )

    def filter_mask(self, mask: list[bool]) -> ColumnVector:
        """A new vector keeping rows where ``mask[i]`` is True."""
        if len(mask) != len(self._values):
            raise ValueError("mask length must equal vector length")
        idx = [i for i, keep in enumerate(mask) if keep]
        return self.take(idx)

    def append(self, other: ColumnVector) -> ColumnVector:
        """Concatenate two same-typed vectors into a new one."""
        if other.dtype is not self.dtype:
            raise TypeError(f"cannot append {other.dtype} to {self.dtype}")
        return ColumnVector(
            self.dtype, self._values + other._values, self._valid + other._valid
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ColumnVector):
            return NotImplemented
        return (
            self.dtype is other.dtype
            and self._values == other._values
            and self._valid == other._valid
        )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"ColumnVector({self.dtype}, {self.to_pylist()!r})"


__all__ = [
    "ColumnVector",
    "Field",
    "LogicalType",
    "Schema",
    "placeholder_for",
]
