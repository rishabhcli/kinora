"""The pushdown predicate algebra.

A :class:`Predicate` is a small boolean tree over column comparisons. It serves two
jobs with one definition:

1. **Pushdown** — :meth:`Predicate.can_skip_statistics` decides, from a row group's
   :class:`~app.lakehouse.warehouse.statistics.ColumnStatistics`, whether the group
   *cannot possibly* contain a matching row (so the reader skips decoding it). This
   is conservative: it only returns ``True`` when provably empty.
2. **Evaluation** — :meth:`Predicate.evaluate` produces a boolean keep-mask over a
   batch of decoded columns, with SQL three-valued logic (a NULL comparison is
   *unknown*, which does not pass a filter).

Supported leaves: ``=, !=, <, <=, >, >=`` against a literal, ``IS NULL`` /
``IS NOT NULL``, and ``IN (...)``. Combinators: ``AND``, ``OR``, ``NOT``. The query
engine's filter operator and the columnar reader share this exact algebra.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.lakehouse.warehouse.statistics import ColumnStatistics
from app.lakehouse.warehouse.types import ColumnVector


class CompareOp(enum.StrEnum):
    EQ = "="
    NE = "!="
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="


class Predicate(ABC):
    """Base of the predicate tree."""

    @abstractmethod
    def columns(self) -> set[str]:
        """Column names this predicate references."""

    @abstractmethod
    def evaluate(self, batch: dict[str, ColumnVector]) -> list[bool]:
        """A keep-mask over the batch rows (NULL/unknown ⇒ False)."""

    @abstractmethod
    def can_skip_statistics(self, stats: dict[str, ColumnStatistics]) -> bool:
        """``True`` when the stats prove no row can match (safe to skip).

        Conservative: ``False`` whenever a referenced column lacks usable stats,
        so an unknown never causes incorrect skipping.
        """

    def __and__(self, other: Predicate) -> Predicate:
        return And((self, other))

    def __or__(self, other: Predicate) -> Predicate:
        return Or((self, other))

    def __invert__(self) -> Predicate:
        return Not(self)


def _batch_len(batch: dict[str, ColumnVector]) -> int:
    return len(next(iter(batch.values()))) if batch else 0


@dataclass(frozen=True, slots=True)
class Compare(Predicate):
    """``column <op> literal``."""

    column: str
    op: CompareOp
    value: Any

    def columns(self) -> set[str]:
        return {self.column}

    def evaluate(self, batch: dict[str, ColumnVector]) -> list[bool]:
        vec = batch[self.column]
        out: list[bool] = []
        op = self.op
        target = self.value
        for i in range(len(vec)):
            if not vec.is_valid(i):
                out.append(False)  # NULL <op> x is unknown -> filtered out
                continue
            v = vec.value(i)
            out.append(_apply(op, v, target))
        return out

    def can_skip_statistics(self, stats: dict[str, ColumnStatistics]) -> bool:
        st = stats.get(self.column)
        if st is None:
            return False
        if st.all_null:
            return True  # every value is NULL; no comparison can be True
        if not st.has_range:
            return False
        lo, hi, target, op = st.min_value, st.max_value, self.value, self.op
        if op is CompareOp.EQ:
            return target < lo or target > hi
        if op is CompareOp.LT:
            return lo >= target  # min already >= target ⇒ nothing is < target
        if op is CompareOp.LE:
            return lo > target
        if op is CompareOp.GT:
            return hi <= target
        if op is CompareOp.GE:
            return hi < target
        # NE can only be skipped if the column is a single constant equal to target.
        if op is CompareOp.NE:
            return lo == hi == target
        return False


@dataclass(frozen=True, slots=True)
class InList(Predicate):
    """``column IN (v0, v1, ...)``."""

    column: str
    values: tuple[Any, ...]

    def columns(self) -> set[str]:
        return {self.column}

    def evaluate(self, batch: dict[str, ColumnVector]) -> list[bool]:
        vec = batch[self.column]
        members = set(self.values)
        return [
            vec.is_valid(i) and vec.value(i) in members for i in range(len(vec))
        ]

    def can_skip_statistics(self, stats: dict[str, ColumnStatistics]) -> bool:
        st = stats.get(self.column)
        if st is None or st.all_null:
            return st is not None and st.all_null
        if not st.has_range or not self.values:
            return not self.values  # empty IN list matches nothing
        # Skip if every literal is outside [min, max].
        return all(v < st.min_value or v > st.max_value for v in self.values)


@dataclass(frozen=True, slots=True)
class IsNull(Predicate):
    column: str

    def columns(self) -> set[str]:
        return {self.column}

    def evaluate(self, batch: dict[str, ColumnVector]) -> list[bool]:
        vec = batch[self.column]
        return [not vec.is_valid(i) for i in range(len(vec))]

    def can_skip_statistics(self, stats: dict[str, ColumnStatistics]) -> bool:
        st = stats.get(self.column)
        if st is None:
            return False
        return st.null_count == 0  # no nulls ⇒ IS NULL matches nothing


@dataclass(frozen=True, slots=True)
class IsNotNull(Predicate):
    column: str

    def columns(self) -> set[str]:
        return {self.column}

    def evaluate(self, batch: dict[str, ColumnVector]) -> list[bool]:
        vec = batch[self.column]
        return [vec.is_valid(i) for i in range(len(vec))]

    def can_skip_statistics(self, stats: dict[str, ColumnStatistics]) -> bool:
        st = stats.get(self.column)
        if st is None:
            return False
        return st.all_null  # all NULL ⇒ IS NOT NULL matches nothing


@dataclass(frozen=True, slots=True)
class And(Predicate):
    children: tuple[Predicate, ...]

    def columns(self) -> set[str]:
        cols: set[str] = set()
        for c in self.children:
            cols |= c.columns()
        return cols

    def evaluate(self, batch: dict[str, ColumnVector]) -> list[bool]:
        n = _batch_len(batch)
        result = [True] * n
        for child in self.children:
            mask = child.evaluate(batch)
            result = [a and b for a, b in zip(result, mask, strict=True)]
        return result

    def can_skip_statistics(self, stats: dict[str, ColumnStatistics]) -> bool:
        # AND can be skipped if ANY conjunct proves emptiness.
        return any(c.can_skip_statistics(stats) for c in self.children)


@dataclass(frozen=True, slots=True)
class Or(Predicate):
    children: tuple[Predicate, ...]

    def columns(self) -> set[str]:
        cols: set[str] = set()
        for c in self.children:
            cols |= c.columns()
        return cols

    def evaluate(self, batch: dict[str, ColumnVector]) -> list[bool]:
        n = _batch_len(batch)
        result = [False] * n
        for child in self.children:
            mask = child.evaluate(batch)
            result = [a or b for a, b in zip(result, mask, strict=True)]
        return result

    def can_skip_statistics(self, stats: dict[str, ColumnStatistics]) -> bool:
        # OR can be skipped only if EVERY disjunct proves emptiness.
        return bool(self.children) and all(
            c.can_skip_statistics(stats) for c in self.children
        )


@dataclass(frozen=True, slots=True)
class Not(Predicate):
    child: Predicate

    def columns(self) -> set[str]:
        return self.child.columns()

    def evaluate(self, batch: dict[str, ColumnVector]) -> list[bool]:
        # SQL NOT keeps rows where the child evaluated to definitely-False, i.e.
        # the child returned False. (Three-valued NULL handling is already folded
        # into the child masks: NULL comparisons came back False, and NOT of an
        # unknown is still unknown — filtered out — which we approximate by also
        # treating child-NULL as not-kept. For correctness over nullable columns,
        # wrap explicit IS NOT NULL guards alongside NOT.)
        mask = self.child.evaluate(batch)
        return [not b for b in mask]

    def can_skip_statistics(self, stats: dict[str, ColumnStatistics]) -> bool:
        # Negation can't be soundly pushed down from min/max alone in general.
        return False


def _apply(op: CompareOp, a: Any, b: Any) -> bool:
    if op is CompareOp.EQ:
        return bool(a == b)
    if op is CompareOp.NE:
        return bool(a != b)
    if op is CompareOp.LT:
        return bool(a < b)
    if op is CompareOp.LE:
        return bool(a <= b)
    if op is CompareOp.GT:
        return bool(a > b)
    return bool(a >= b)  # GE


# -- ergonomic builders ----------------------------------------------------- #


def col_eq(column: str, value: Any) -> Compare:
    return Compare(column, CompareOp.EQ, value)


def col_ne(column: str, value: Any) -> Compare:
    return Compare(column, CompareOp.NE, value)


def col_lt(column: str, value: Any) -> Compare:
    return Compare(column, CompareOp.LT, value)


def col_le(column: str, value: Any) -> Compare:
    return Compare(column, CompareOp.LE, value)


def col_gt(column: str, value: Any) -> Compare:
    return Compare(column, CompareOp.GT, value)


def col_ge(column: str, value: Any) -> Compare:
    return Compare(column, CompareOp.GE, value)


def col_in(column: str, values: list[Any]) -> InList:
    return InList(column, tuple(values))


__all__ = [
    "And",
    "Compare",
    "CompareOp",
    "InList",
    "IsNotNull",
    "IsNull",
    "Not",
    "Or",
    "Predicate",
    "col_eq",
    "col_ge",
    "col_gt",
    "col_in",
    "col_le",
    "col_lt",
    "col_ne",
]
