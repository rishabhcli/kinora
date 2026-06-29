"""Aggregate functions and their accumulators.

An :class:`AggregateSpec` names an aggregate over an input expression and the output
column name. An :class:`Accumulator` folds a stream of (value, valid) cells into a
running state and finalises to a single (value, valid) cell — the abstraction the
hash-aggregate and global-aggregate physical operators share, one accumulator per
group.

Supported aggregates: ``COUNT(*)``, ``COUNT(expr)`` (non-null), ``COUNT(DISTINCT
expr)``, ``SUM``, ``MIN``, ``MAX``, ``AVG``. NULL inputs are ignored by all except
``COUNT(*)``. The result type follows SQL: ``COUNT`` → INT64, ``AVG`` → FLOAT64,
``SUM`` preserves integer/float, ``MIN``/``MAX`` preserve the input type.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.lakehouse.warehouse.expr import Expr
from app.lakehouse.warehouse.types import LogicalType


class AggFunc(enum.StrEnum):
    COUNT = "count"
    COUNT_STAR = "count_star"
    COUNT_DISTINCT = "count_distinct"
    SUM = "sum"
    MIN = "min"
    MAX = "max"
    AVG = "avg"


@dataclass(frozen=True, slots=True)
class AggregateSpec:
    """One aggregate output: ``func(input) AS output_name``.

    ``input`` is ``None`` for ``COUNT(*)``.
    """

    func: AggFunc
    output_name: str
    input: Expr | None = None

    def result_type(self, schema_types: dict[str, LogicalType]) -> LogicalType:
        if self.func in (AggFunc.COUNT, AggFunc.COUNT_STAR, AggFunc.COUNT_DISTINCT):
            return LogicalType.INT64
        if self.func is AggFunc.AVG:
            return LogicalType.FLOAT64
        if self.input is None:  # pragma: no cover - guarded by builders
            raise ValueError(f"{self.func} requires an input expression")
        it = self.input.result_type(schema_types)
        if self.func is AggFunc.SUM and it.is_integer:
            return LogicalType.INT64
        if self.func is AggFunc.SUM:
            return LogicalType.FLOAT64
        return it  # MIN / MAX preserve type

    def new_accumulator(self) -> Accumulator:
        return _ACCUMULATORS[self.func]()


class Accumulator(ABC):
    """A foldable aggregate state for one group."""

    @abstractmethod
    def update(self, value: Any, valid: bool) -> None: ...

    @abstractmethod
    def result(self) -> tuple[Any, bool]:
        """Return ``(value, valid)`` (``valid=False`` is NULL)."""


class _CountStar(Accumulator):
    def __init__(self) -> None:
        self._n = 0

    def update(self, value: Any, valid: bool) -> None:
        self._n += 1

    def result(self) -> tuple[Any, bool]:
        return self._n, True


class _Count(Accumulator):
    def __init__(self) -> None:
        self._n = 0

    def update(self, value: Any, valid: bool) -> None:
        if valid:
            self._n += 1

    def result(self) -> tuple[Any, bool]:
        return self._n, True


class _CountDistinct(Accumulator):
    def __init__(self) -> None:
        self._seen: set[Any] = set()

    def update(self, value: Any, valid: bool) -> None:
        if valid:
            self._seen.add(value)

    def result(self) -> tuple[Any, bool]:
        return len(self._seen), True


class _Sum(Accumulator):
    def __init__(self) -> None:
        self._total: Any = None

    def update(self, value: Any, valid: bool) -> None:
        if valid:
            self._total = value if self._total is None else self._total + value

    def result(self) -> tuple[Any, bool]:
        return (self._total, True) if self._total is not None else (0, False)


class _Min(Accumulator):
    def __init__(self) -> None:
        self._v: Any = None

    def update(self, value: Any, valid: bool) -> None:
        if valid and (self._v is None or value < self._v):
            self._v = value

    def result(self) -> tuple[Any, bool]:
        return (self._v, True) if self._v is not None else (None, False)


class _Max(Accumulator):
    def __init__(self) -> None:
        self._v: Any = None

    def update(self, value: Any, valid: bool) -> None:
        if valid and (self._v is None or value > self._v):
            self._v = value

    def result(self) -> tuple[Any, bool]:
        return (self._v, True) if self._v is not None else (None, False)


class _Avg(Accumulator):
    def __init__(self) -> None:
        self._sum = 0.0
        self._n = 0

    def update(self, value: Any, valid: bool) -> None:
        if valid:
            self._sum += value
            self._n += 1

    def result(self) -> tuple[Any, bool]:
        return (self._sum / self._n, True) if self._n else (None, False)


_ACCUMULATORS: dict[AggFunc, type[Accumulator]] = {
    AggFunc.COUNT_STAR: _CountStar,
    AggFunc.COUNT: _Count,
    AggFunc.COUNT_DISTINCT: _CountDistinct,
    AggFunc.SUM: _Sum,
    AggFunc.MIN: _Min,
    AggFunc.MAX: _Max,
    AggFunc.AVG: _Avg,
}


# -- ergonomic builders ----------------------------------------------------- #


def count_star(name: str = "count") -> AggregateSpec:
    return AggregateSpec(AggFunc.COUNT_STAR, name)


def count(expr: Expr, name: str | None = None) -> AggregateSpec:
    return AggregateSpec(AggFunc.COUNT, name or f"count_{expr.to_name()}", expr)


def count_distinct(expr: Expr, name: str | None = None) -> AggregateSpec:
    return AggregateSpec(AggFunc.COUNT_DISTINCT, name or f"count_distinct_{expr.to_name()}", expr)


def sum_(expr: Expr, name: str | None = None) -> AggregateSpec:
    return AggregateSpec(AggFunc.SUM, name or f"sum_{expr.to_name()}", expr)


def min_(expr: Expr, name: str | None = None) -> AggregateSpec:
    return AggregateSpec(AggFunc.MIN, name or f"min_{expr.to_name()}", expr)


def max_(expr: Expr, name: str | None = None) -> AggregateSpec:
    return AggregateSpec(AggFunc.MAX, name or f"max_{expr.to_name()}", expr)


def avg(expr: Expr, name: str | None = None) -> AggregateSpec:
    return AggregateSpec(AggFunc.AVG, name or f"avg_{expr.to_name()}", expr)


__all__ = [
    "Accumulator",
    "AggFunc",
    "AggregateSpec",
    "avg",
    "count",
    "count_distinct",
    "count_star",
    "max_",
    "min_",
    "sum_",
]
