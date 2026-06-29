"""Incrementally-maintained aggregate views (GROUP BY + reduce).

Aggregation is the hard IVM case: a single source-row change shifts a *group's*
aggregate, and a group can appear or vanish. We maintain it correctly and
incrementally using per-group reducers that support **retraction** (subtracting
a row's contribution), which is exactly what the Z-set delta model gives us.

:class:`AggregateView` groups projected source rows by a key, folds each group's
member rows through a :class:`Reducer`, and emits one output row per non-empty
group. A :class:`Reducer` is an incremental fold: ``add(acc, row)`` and the
inverse ``remove(acc, row)`` plus ``finalize(acc) -> value``. We ship the common
ones:

* :class:`CountReducer` — row count.
* :class:`SumReducer` / :class:`AvgReducer` — over a numeric column.
* :class:`MinReducer` / :class:`MaxReducer` — order-statistic reducers that keep a
  per-value multiset so a retraction of the current extreme correctly falls back
  to the next one (the classic "min/max under deletes" problem).
* :class:`DistinctCountReducer` — distinct values, multiset-backed.

This powers product read models like "shots per book", "active characters per
book", or "reading progress per session" kept live off the change stream.
"""

from __future__ import annotations

import abc
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from app.streaming.cdc.events import ChangeEvent, key_str
from app.streaming.cdc.views.delta import Delta, Row, ZSet, update_delta
from app.streaming.cdc.views.view import MaterializedView


class Reducer(abc.ABC):
    """An invertible incremental fold over a group's member rows."""

    @abc.abstractmethod
    def initial(self) -> Any:
        """A fresh, empty accumulator."""

    @abc.abstractmethod
    def add(self, acc: Any, row: Mapping[str, Any]) -> Any:
        """Fold ``row`` into ``acc`` (return the new accumulator)."""

    @abc.abstractmethod
    def remove(self, acc: Any, row: Mapping[str, Any]) -> Any:
        """Un-fold ``row`` from ``acc`` (the inverse of :meth:`add`)."""

    @abc.abstractmethod
    def finalize(self, acc: Any) -> Any:
        """Project the accumulator to the emitted aggregate value."""

    @abc.abstractmethod
    def is_empty(self, acc: Any) -> bool:
        """Whether the accumulator holds no rows (group should disappear)."""


class CountReducer(Reducer):
    def initial(self) -> int:
        return 0

    def add(self, acc: int, row: Mapping[str, Any]) -> int:
        return acc + 1

    def remove(self, acc: int, row: Mapping[str, Any]) -> int:
        return acc - 1

    def finalize(self, acc: int) -> int:
        return acc

    def is_empty(self, acc: int) -> bool:
        return acc <= 0


class SumReducer(Reducer):
    def __init__(self, column: str) -> None:
        self.column = column

    def _val(self, row: Mapping[str, Any]) -> float:
        v = row.get(self.column)
        return float(v) if v is not None else 0.0

    def initial(self) -> dict[str, float]:
        return {"sum": 0.0, "n": 0}

    def add(self, acc: dict[str, float], row: Mapping[str, Any]) -> dict[str, float]:
        return {"sum": acc["sum"] + self._val(row), "n": acc["n"] + 1}

    def remove(self, acc: dict[str, float], row: Mapping[str, Any]) -> dict[str, float]:
        return {"sum": acc["sum"] - self._val(row), "n": acc["n"] - 1}

    def finalize(self, acc: dict[str, float]) -> Any:
        return acc["sum"]

    def is_empty(self, acc: dict[str, float]) -> bool:
        return acc["n"] <= 0


class AvgReducer(SumReducer):
    def finalize(self, acc: dict[str, float]) -> Any:
        return acc["sum"] / acc["n"] if acc["n"] else None


class _MultisetExtremeReducer(Reducer):
    """Base for min/max: keep a multiset of values so deletes fall back correctly."""

    def __init__(self, column: str, *, pick: Callable[[Iterable[Any]], Any]) -> None:
        self.column = column
        self._pick = pick

    def initial(self) -> dict[Any, int]:
        return {}

    def add(self, acc: dict[Any, int], row: Mapping[str, Any]) -> dict[Any, int]:
        v = row.get(self.column)
        out = dict(acc)
        out[v] = out.get(v, 0) + 1
        return out

    def remove(self, acc: dict[Any, int], row: Mapping[str, Any]) -> dict[Any, int]:
        v = row.get(self.column)
        out = dict(acc)
        n = out.get(v, 0) - 1
        if n <= 0:
            out.pop(v, None)
        else:
            out[v] = n
        return out

    def finalize(self, acc: dict[Any, int]) -> Any:
        present = [v for v in acc if v is not None]
        return self._pick(present) if present else None

    def is_empty(self, acc: dict[Any, int]) -> bool:
        return not acc


class MinReducer(_MultisetExtremeReducer):
    def __init__(self, column: str) -> None:
        super().__init__(column, pick=min)


class MaxReducer(_MultisetExtremeReducer):
    def __init__(self, column: str) -> None:
        super().__init__(column, pick=max)


class DistinctCountReducer(Reducer):
    def __init__(self, column: str) -> None:
        self.column = column

    def initial(self) -> dict[Any, int]:
        return {}

    def add(self, acc: dict[Any, int], row: Mapping[str, Any]) -> dict[Any, int]:
        v = row.get(self.column)
        out = dict(acc)
        out[v] = out.get(v, 0) + 1
        return out

    def remove(self, acc: dict[Any, int], row: Mapping[str, Any]) -> dict[Any, int]:
        v = row.get(self.column)
        out = dict(acc)
        n = out.get(v, 0) - 1
        if n <= 0:
            out.pop(v, None)
        else:
            out[v] = n
        return out

    def finalize(self, acc: dict[Any, int]) -> int:
        return len(acc)

    def is_empty(self, acc: dict[Any, int]) -> bool:
        return not acc


class AggregateView(MaterializedView):
    """A GROUP BY aggregate over one source table, maintained incrementally.

    Subclass or instantiate with:

    * ``source`` — the base table,
    * ``group_by`` — the columns forming the group key,
    * ``aggregates`` — ``{output_name: Reducer}``,
    * optional ``where`` — a predicate over the base row (rows that fail it don't
      contribute; flipping the predicate value moves a row between contributing
      and not, handled as a synthetic add/remove).

    Each non-empty group emits one output row: the group-by columns + each
    finalized aggregate. The view tracks per-group accumulators and the last
    contributing image of each source key so an UPDATE (no before-image from
    polling) still retracts the prior contribution.
    """

    def __init__(
        self,
        *,
        name: str,
        source: str,
        group_by: tuple[str, ...],
        aggregates: Mapping[str, Reducer],
        where: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> None:
        self.name = name
        self._source = source
        self._group_by = group_by
        self._aggregates = dict(aggregates)
        self._where = where or (lambda _row: True)
        self._state = ZSet()
        # group key str -> {agg_name: accumulator}
        self._acc: dict[str, dict[str, Any]] = {}
        # group key str -> the group_by cell values (to rebuild the output row)
        self._group_cells: dict[str, dict[str, Any]] = {}
        # source key str -> last contributing row image (for retraction)
        self._contributing: dict[str, Mapping[str, Any]] = {}

    @property
    def sources(self) -> tuple[str, ...]:
        return (self._source,)

    @property
    def state(self) -> ZSet:
        return self._state

    # -- incremental maintenance ------------------------------------------- #
    def on_event(self, event: ChangeEvent) -> Delta:
        if event.table != self._source or not event.is_row_event:
            return ZSet()
        skey = key_str(event.key)
        old_row = self._contributing.get(skey)
        new_row = None
        if not event.is_delete:
            body = event.after or {}
            if self._where(body):
                new_row = body

        delta = ZSet()
        # Retract the old contribution (if any), then add the new one.
        if old_row is not None:
            delta += self._apply_to_group(old_row, sign=-1)
            del self._contributing[skey]
        if new_row is not None:
            delta += self._apply_to_group(new_row, sign=+1)
            self._contributing[skey] = dict(new_row)
        return delta

    def _group_key(self, row: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        cells = {c: row.get(c) for c in self._group_by}
        return key_str(cells), cells

    def _apply_to_group(self, row: Mapping[str, Any], *, sign: int) -> Delta:
        gkey, cells = self._group_key(row)
        old_out = self._output_row(gkey)

        acc = self._acc.get(gkey) or {n: r.initial() for n, r in self._aggregates.items()}
        for agg_name, reducer in self._aggregates.items():
            if sign > 0:
                acc[agg_name] = reducer.add(acc[agg_name], row)
            else:
                acc[agg_name] = reducer.remove(acc[agg_name], row)

        empty = all(r.is_empty(acc[n]) for n, r in self._aggregates.items())
        if empty:
            self._acc.pop(gkey, None)
            self._group_cells.pop(gkey, None)
        else:
            self._acc[gkey] = acc
            self._group_cells[gkey] = cells

        new_out = self._output_row(gkey)
        return update_delta(old_out, new_out)

    def _output_row(self, gkey: str) -> Row | None:
        acc = self._acc.get(gkey)
        if acc is None:
            return None
        cells = dict(self._group_cells[gkey])
        for agg_name, reducer in self._aggregates.items():
            cells[agg_name] = reducer.finalize(acc[agg_name])
        return Row(cells)

    # -- consistency oracle ------------------------------------------------- #
    def recompute(self, base: Mapping[str, Iterable[Mapping[str, Any]]]) -> ZSet:
        groups: dict[str, dict[str, Any]] = {}
        group_cells: dict[str, dict[str, Any]] = {}
        for row in base.get(self._source, []):
            if not self._where(row):
                continue
            gkey, cells = self._group_key(row)
            acc = groups.get(gkey) or {n: r.initial() for n, r in self._aggregates.items()}
            for agg_name, reducer in self._aggregates.items():
                acc[agg_name] = reducer.add(acc[agg_name], row)
            groups[gkey] = acc
            group_cells[gkey] = cells
        out = ZSet()
        for gkey, acc in groups.items():
            if all(r.is_empty(acc[n]) for n, r in self._aggregates.items()):
                continue
            cells = dict(group_cells[gkey])
            for agg_name, reducer in self._aggregates.items():
                cells[agg_name] = reducer.finalize(acc[agg_name])
            out.add(Row(cells), +1)
        return out


__all__ = [
    "AggregateView",
    "AvgReducer",
    "CountReducer",
    "DistinctCountReducer",
    "MaxReducer",
    "MinReducer",
    "Reducer",
    "SumReducer",
]
