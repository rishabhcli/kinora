"""The delta algebra incremental view maintenance is built on.

Incremental maintenance needs a way to say "this view changed by *this much*"
without recomputing it. We use the standard **Z-set** (weighted multiset)
model from differential/incremental view-maintenance theory:

* A :class:`Row` is an immutable, hashable tuple of named cells.
* A :class:`ZSet` maps rows to integer **weights**. ``+1`` = present once,
  ``+2`` = present twice (a bag), ``-1`` = a retraction. A consistent view
  state has all weights ``>= 0``.
* A :class:`Delta` is just a :class:`ZSet` interpreted as a *change*: an insert
  is ``{row: +1}``, a delete is ``{row: -1}``, and an update is the two composed
  (``{old: -1, new: +1}``). Applying a delta is Z-set addition; weights that hit
  zero drop out. This makes update/insert/delete *uniform* — the view never
  special-cases them — and makes maintenance associative and commutative, which
  is what lets the engine batch and replay safely.

These are pure, dependency-free value types so the whole IVM core is trivially
unit-testable.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any


class Row(tuple[tuple[str, Any], ...]):
    """An immutable, hashable row: an ordered tuple of ``(column, value)`` pairs.

    Built from a mapping; cells are sorted by column so two logically-equal rows
    hash equal regardless of insertion order. Values must themselves be
    hashable (scalars / tuples) — JSON-y nested structures are normalised to a
    hashable form by :func:`freeze`.
    """

    __slots__ = ()

    def __new__(cls, mapping: Mapping[str, Any]) -> Row:
        items = tuple(sorted((k, freeze(v)) for k, v in mapping.items()))
        return super().__new__(cls, items)

    def as_dict(self) -> dict[str, Any]:
        return dict(self)

    def get(self, column: str, default: Any = None) -> Any:
        for k, v in self:
            if k == column:
                return v
        return default


def freeze(value: Any) -> Any:
    """Recursively convert a JSON-ish value into a hashable form."""
    if isinstance(value, Mapping):
        return tuple(sorted((k, freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    if isinstance(value, set):
        return tuple(sorted(freeze(v) for v in value))
    return value


class ZSet:
    """A weighted multiset of rows — the unit of view state and view change.

    Addition merges weights; rows whose weight reaches zero are pruned, so the
    representation is canonical (two equal Z-sets compare equal).
    """

    __slots__ = ("_w",)

    def __init__(self, weights: Mapping[Row, int] | None = None) -> None:
        self._w: dict[Row, int] = {}
        if weights:
            for row, w in weights.items():
                if w:
                    self._w[row] = w

    # -- constructors ------------------------------------------------------- #
    @classmethod
    def singleton(cls, row: Row, weight: int = 1) -> ZSet:
        return cls({row: weight})

    @classmethod
    def from_rows(cls, rows: Iterable[Row], weight: int = 1) -> ZSet:
        z = cls()
        for r in rows:
            z.add(r, weight)
        return z

    # -- mutation ----------------------------------------------------------- #
    def add(self, row: Row, weight: int = 1) -> None:
        new = self._w.get(row, 0) + weight
        if new == 0:
            self._w.pop(row, None)
        else:
            self._w[row] = new

    def __iadd__(self, other: ZSet) -> ZSet:
        for row, w in other._w.items():
            self.add(row, w)
        return self

    def __add__(self, other: ZSet) -> ZSet:
        out = ZSet(self._w)
        out += other
        return out

    def negate(self) -> ZSet:
        return ZSet({row: -w for row, w in self._w.items()})

    def filter(self, predicate: Any) -> ZSet:
        """A Z-set of the rows matching ``predicate`` (weights preserved)."""
        return ZSet({row: w for row, w in self._w.items() if predicate(row)})

    # -- views -------------------------------------------------------------- #
    def weight(self, row: Row) -> int:
        return self._w.get(row, 0)

    def rows(self) -> list[Row]:
        """Distinct rows with positive weight (the materialised content)."""
        return [r for r, w in self._w.items() if w > 0]

    def items(self) -> Iterator[tuple[Row, int]]:
        return iter(self._w.items())

    def is_consistent(self) -> bool:
        """A real view never holds a negative weight (no phantom retraction)."""
        return all(w >= 0 for w in self._w.values())

    def __len__(self) -> int:
        return len(self._w)

    def __bool__(self) -> bool:
        return bool(self._w)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ZSet) and self._w == other._w

    def __hash__(self) -> int:  # pragma: no cover - Z-sets are mutable; hash by id intent
        raise TypeError("ZSet is mutable and unhashable")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"ZSet({self._w!r})"


#: A :class:`Delta` *is* a :class:`ZSet`; the alias documents intent at call
#: sites (a value used as a change vs. as accumulated state).
Delta = ZSet


def insert_delta(row: Row) -> Delta:
    return ZSet.singleton(row, +1)


def delete_delta(row: Row) -> Delta:
    return ZSet.singleton(row, -1)


def update_delta(old: Row | None, new: Row | None) -> Delta:
    """The delta that retracts ``old`` and asserts ``new`` (either may be ``None``)."""
    d = ZSet()
    if old is not None:
        d.add(old, -1)
    if new is not None:
        d.add(new, +1)
    return d


__all__ = [
    "Delta",
    "Row",
    "ZSet",
    "delete_delta",
    "freeze",
    "insert_delta",
    "update_delta",
]
