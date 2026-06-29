"""Scalar expressions evaluated vectorized over a record batch.

An :class:`Expr` evaluates to a :class:`~app.lakehouse.warehouse.types.ColumnVector`
given a :class:`~app.lakehouse.warehouse.batch.RecordBatch`. The expression tree is
what ``project`` computes and what ``filter`` evaluates (a boolean expression). It
also carries a :meth:`Expr.result_type` so the planner can type-check and name
output columns.

Supported nodes:

* :class:`Column` — reference a column by name.
* :class:`Literal` — a constant of a given type.
* :class:`Arithmetic` — ``+ - * /`` over numeric columns (NULL-propagating).
* :class:`Comparison` — ``= != < <= > >=`` → BOOL (SQL three-valued: NULL operand ⇒
  NULL result, which a filter treats as not-passing).
* :class:`BoolOp` — ``AND`` / ``OR`` / ``NOT`` over BOOL columns.
* :class:`Cast` — change logical type (numeric widening / narrowing, to-string).
* :class:`Coalesce` — first non-null of its arguments.

Kernels are intentionally simple Python loops over the vectors — clear and
exhaustively testable; this is a *mini* engine, not a JIT.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.lakehouse.warehouse.batch import RecordBatch
from app.lakehouse.warehouse.types import ColumnVector, LogicalType, placeholder_for


class Expr(ABC):
    """A scalar expression evaluable vectorized over a batch."""

    @abstractmethod
    def evaluate(self, batch: RecordBatch) -> ColumnVector: ...

    @abstractmethod
    def result_type(self, schema_types: dict[str, LogicalType]) -> LogicalType: ...

    @abstractmethod
    def columns(self) -> set[str]: ...

    @abstractmethod
    def to_name(self) -> str:
        """A default output column name."""


@dataclass(frozen=True, slots=True)
class Column(Expr):
    name: str

    def evaluate(self, batch: RecordBatch) -> ColumnVector:
        return batch.column(self.name)

    def result_type(self, schema_types: dict[str, LogicalType]) -> LogicalType:
        return schema_types[self.name]

    def columns(self) -> set[str]:
        return {self.name}

    def to_name(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class Literal(Expr):
    value: Any
    dtype: LogicalType

    def evaluate(self, batch: RecordBatch) -> ColumnVector:
        n = batch.num_rows
        return ColumnVector.from_pylist(self.dtype, [self.value] * n)

    def result_type(self, schema_types: dict[str, LogicalType]) -> LogicalType:
        return self.dtype

    def columns(self) -> set[str]:
        return set()

    def to_name(self) -> str:
        return f"lit({self.value})"


class ArithOp(enum.StrEnum):
    ADD = "+"
    SUB = "-"
    MUL = "*"
    DIV = "/"


@dataclass(frozen=True, slots=True)
class Arithmetic(Expr):
    op: ArithOp
    left: Expr
    right: Expr

    def evaluate(self, batch: RecordBatch) -> ColumnVector:
        lhs = self.left.evaluate(batch)
        rhs = self.right.evaluate(batch)
        out_type = self._out_type(lhs.dtype, rhs.dtype)
        ph = placeholder_for(out_type)
        values: list[Any] = []
        valid: list[bool] = []
        for i in range(len(lhs)):
            if not (lhs.is_valid(i) and rhs.is_valid(i)):
                values.append(ph)
                valid.append(False)
                continue
            res = self._apply(lhs.value(i), rhs.value(i))
            if res is None:  # e.g. division by zero -> NULL (SQL-ish)
                values.append(ph)
                valid.append(False)
            else:
                values.append(int(res) if out_type.is_integer else float(res))
                valid.append(True)
        return ColumnVector(out_type, values, valid)

    def _apply(self, a: Any, b: Any) -> Any:
        if self.op is ArithOp.ADD:
            return a + b
        if self.op is ArithOp.SUB:
            return a - b
        if self.op is ArithOp.MUL:
            return a * b
        return None if b == 0 else a / b  # DIV

    def _out_type(self, lt: LogicalType, rt: LogicalType) -> LogicalType:
        # SQL ``/`` is true division, so it always yields a float (and avoids the
        # ``int(0.1) == 0`` truncation trap). ``+ - *`` over two integers stay int.
        if self.op is ArithOp.DIV:
            return LogicalType.FLOAT64
        if lt.is_floating or rt.is_floating:
            return LogicalType.FLOAT64
        return LogicalType.INT64

    def result_type(self, schema_types: dict[str, LogicalType]) -> LogicalType:
        return self._out_type(
            self.left.result_type(schema_types), self.right.result_type(schema_types)
        )

    def columns(self) -> set[str]:
        return self.left.columns() | self.right.columns()

    def to_name(self) -> str:
        return f"({self.left.to_name()}{self.op}{self.right.to_name()})"


class CompareOp(enum.StrEnum):
    EQ = "="
    NE = "!="
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="


@dataclass(frozen=True, slots=True)
class Comparison(Expr):
    op: CompareOp
    left: Expr
    right: Expr

    def evaluate(self, batch: RecordBatch) -> ColumnVector:
        lhs = self.left.evaluate(batch)
        rhs = self.right.evaluate(batch)
        values: list[Any] = []
        valid: list[bool] = []
        for i in range(len(lhs)):
            if not (lhs.is_valid(i) and rhs.is_valid(i)):
                values.append(False)
                valid.append(False)  # NULL comparison -> unknown
                continue
            values.append(self._apply(lhs.value(i), rhs.value(i)))
            valid.append(True)
        return ColumnVector(LogicalType.BOOL, values, valid)

    def _apply(self, a: Any, b: Any) -> bool:
        op = self.op
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
        return bool(a >= b)

    def result_type(self, schema_types: dict[str, LogicalType]) -> LogicalType:
        return LogicalType.BOOL

    def columns(self) -> set[str]:
        return self.left.columns() | self.right.columns()

    def to_name(self) -> str:
        return f"({self.left.to_name()}{self.op}{self.right.to_name()})"


class BoolKind(enum.StrEnum):
    AND = "and"
    OR = "or"
    NOT = "not"


@dataclass(frozen=True, slots=True)
class BoolOp(Expr):
    kind: BoolKind
    operands: tuple[Expr, ...]

    def evaluate(self, batch: RecordBatch) -> ColumnVector:
        vecs = [op.evaluate(batch) for op in self.operands]
        n = batch.num_rows
        values: list[Any] = []
        valid: list[bool] = []
        for i in range(n):
            cells = [(v.value(i), v.is_valid(i)) for v in vecs]
            res, ok = self._combine(cells)
            values.append(res if ok else False)
            valid.append(ok)
        return ColumnVector(LogicalType.BOOL, values, valid)

    def _combine(self, cells: list[tuple[Any, bool]]) -> tuple[bool, bool]:
        """Return ``(value, valid)`` under SQL three-valued logic.

        ``valid=False`` is NULL/unknown. AND short-circuits on a definite False;
        OR short-circuits on a definite True; otherwise a NULL operand makes the
        result NULL.
        """
        if self.kind is BoolKind.NOT:
            val, ok = cells[0]
            return (not val, True) if ok else (False, False)
        if self.kind is BoolKind.AND:
            if any(ok and not val for val, ok in cells):
                return False, True  # a definite False dominates
            if any(not ok for _val, ok in cells):
                return False, False  # otherwise any NULL -> NULL
            return True, True
        # OR
        if any(ok and val for val, ok in cells):
            return True, True  # a definite True dominates
        if any(not ok for _val, ok in cells):
            return False, False  # otherwise any NULL -> NULL
        return False, True

    def result_type(self, schema_types: dict[str, LogicalType]) -> LogicalType:
        return LogicalType.BOOL

    def columns(self) -> set[str]:
        cols: set[str] = set()
        for op in self.operands:
            cols |= op.columns()
        return cols

    def to_name(self) -> str:
        return f"{self.kind}({','.join(o.to_name() for o in self.operands)})"


@dataclass(frozen=True, slots=True)
class Cast(Expr):
    child: Expr
    dtype: LogicalType

    def evaluate(self, batch: RecordBatch) -> ColumnVector:
        src = self.child.evaluate(batch)
        ph = placeholder_for(self.dtype)
        values: list[Any] = []
        valid: list[bool] = []
        for i in range(len(src)):
            if not src.is_valid(i):
                values.append(ph)
                valid.append(False)
                continue
            values.append(self._cast(src.value(i)))
            valid.append(True)
        return ColumnVector(self.dtype, values, valid)

    def _cast(self, value: Any) -> Any:
        dt = self.dtype
        if dt.is_integer:
            return int(value)
        if dt.is_floating:
            return float(value)
        if dt is LogicalType.STRING:
            return str(value)
        if dt is LogicalType.BOOL:
            return bool(value)
        return value

    def result_type(self, schema_types: dict[str, LogicalType]) -> LogicalType:
        return self.dtype

    def columns(self) -> set[str]:
        return self.child.columns()

    def to_name(self) -> str:
        return f"cast({self.child.to_name()},{self.dtype})"


@dataclass(frozen=True, slots=True)
class Coalesce(Expr):
    operands: tuple[Expr, ...]

    def evaluate(self, batch: RecordBatch) -> ColumnVector:
        vecs = [op.evaluate(batch) for op in self.operands]
        dtype = vecs[0].dtype
        ph = placeholder_for(dtype)
        values: list[Any] = []
        valid: list[bool] = []
        for i in range(batch.num_rows):
            picked = ph
            ok = False
            for v in vecs:
                if v.is_valid(i):
                    picked = v.value(i)
                    ok = True
                    break
            values.append(picked)
            valid.append(ok)
        return ColumnVector(dtype, values, valid)

    def result_type(self, schema_types: dict[str, LogicalType]) -> LogicalType:
        return self.operands[0].result_type(schema_types)

    def columns(self) -> set[str]:
        cols: set[str] = set()
        for op in self.operands:
            cols |= op.columns()
        return cols

    def to_name(self) -> str:
        return f"coalesce({','.join(o.to_name() for o in self.operands)})"


# -- ergonomic builders ----------------------------------------------------- #


def col(name: str) -> Column:
    return Column(name)


def lit(value: Any, dtype: LogicalType | None = None) -> Literal:
    if dtype is None:
        dtype = _infer_literal_type(value)
    return Literal(value, dtype)


def _infer_literal_type(value: Any) -> LogicalType:
    if isinstance(value, bool):
        return LogicalType.BOOL
    if isinstance(value, int):
        return LogicalType.INT64
    if isinstance(value, float):
        return LogicalType.FLOAT64
    if isinstance(value, str):
        return LogicalType.STRING
    if isinstance(value, bytes):
        return LogicalType.BYTES
    raise TypeError(f"cannot infer literal type for {type(value).__name__}")


def and_(*operands: Expr) -> BoolOp:
    return BoolOp(BoolKind.AND, tuple(operands))


def or_(*operands: Expr) -> BoolOp:
    return BoolOp(BoolKind.OR, tuple(operands))


def not_(operand: Expr) -> BoolOp:
    return BoolOp(BoolKind.NOT, (operand,))


__all__ = [
    "ArithOp",
    "Arithmetic",
    "BoolKind",
    "BoolOp",
    "Cast",
    "Coalesce",
    "Column",
    "CompareOp",
    "Comparison",
    "Expr",
    "Literal",
    "and_",
    "col",
    "lit",
    "not_",
    "or_",
]
