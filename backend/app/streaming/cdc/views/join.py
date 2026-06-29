"""Incrementally-maintained equi-join view (two source tables).

A join is the canonical denormalisation: the read model is one table with
columns from two (or more) sources, so the UI never joins at query time. It is
also the trickiest IVM case — a change on *either* side must update *every*
matching joined row on the other side. The standard technique (and the one used
here) is a **symmetric hash join with delta propagation**:

* Keep a hash index of each side keyed by the join attribute.
* On a left-side delta ``±l``, probe the right index for all matching ``r`` and
  emit ``±(l ⋈ r)``; symmetrically for a right-side delta.
* Updates are a retract of the old image then an assert of the new — composed
  through the same probe, so a join key change correctly *moves* the joined rows
  (retracts the old matches, asserts the new ones).

This is an inner equi-join. The output row is the (optionally renamed) union of
the two projected rows; a ``combine`` hook lets a subclass shape the denormalised
record. ``recompute`` does the from-scratch nested-loop join for the consistency
oracle.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from app.streaming.cdc.events import ChangeEvent, key_str
from app.streaming.cdc.views.delta import Delta, Row, ZSet
from app.streaming.cdc.views.view import MaterializedView


def _default_combine(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    """Union the two rows; right-side columns win on a name clash."""
    out = dict(left)
    out.update(right)
    return out


class EquiJoinView(MaterializedView):
    """Inner equi-join of ``left_table`` and ``right_table`` on a join key.

    Construct with the two tables, the join-key column on each side, the
    primary-key column on each side (for identity within a side), and an optional
    ``combine`` to shape the output row.
    """

    def __init__(
        self,
        *,
        name: str,
        left_table: str,
        right_table: str,
        left_on: str,
        right_on: str,
        left_key: str = "id",
        right_key: str = "id",
        combine: Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.name = name
        self._lt = left_table
        self._rt = right_table
        self._lon = left_on
        self._ron = right_on
        self._lk = left_key
        self._rk = right_key
        self._combine = combine or _default_combine
        self._state = ZSet()
        # join value -> {pk_str: row} for each side
        self._left: dict[Any, dict[str, Mapping[str, Any]]] = {}
        self._right: dict[Any, dict[str, Mapping[str, Any]]] = {}
        # last-seen image per side pk (for update retraction with no before-image)
        self._left_img: dict[str, Mapping[str, Any]] = {}
        self._right_img: dict[str, Mapping[str, Any]] = {}

    @property
    def sources(self) -> tuple[str, ...]:
        return (self._lt, self._rt)

    @property
    def state(self) -> ZSet:
        return self._state

    # -- incremental maintenance ------------------------------------------- #
    def on_event(self, event: ChangeEvent) -> Delta:
        if not event.is_row_event:
            return ZSet()
        if event.table == self._lt:
            return self._on_side(
                event,
                index=self._left,
                images=self._left_img,
                on=self._lon,
                pk=self._lk,
                other_index=self._right,
                left_is_self=True,
            )
        if event.table == self._rt:
            return self._on_side(
                event,
                index=self._right,
                images=self._right_img,
                on=self._ron,
                pk=self._rk,
                other_index=self._left,
                left_is_self=False,
            )
        return ZSet()

    def _on_side(
        self,
        event: ChangeEvent,
        *,
        index: dict[Any, dict[str, Mapping[str, Any]]],
        images: dict[str, Mapping[str, Any]],
        on: str,
        pk: str,
        other_index: dict[Any, dict[str, Mapping[str, Any]]],
        left_is_self: bool,
    ) -> Delta:
        pk_str = key_str(event.key)
        old_img = images.get(pk_str)
        new_img = None if event.is_delete else (event.after or {})

        delta = ZSet()
        # Retract old contribution.
        if old_img is not None:
            self._index_remove(index, old_img.get(on), pk_str)
            delta += self._probe(old_img, other_index, left_is_self, sign=-1)
            del images[pk_str]
        # Assert new contribution.
        if new_img is not None:
            self._index_add(index, new_img.get(on), pk_str, new_img)
            delta += self._probe(new_img, other_index, left_is_self, sign=+1)
            images[pk_str] = dict(new_img)
        return delta

    def _probe(
        self,
        row: Mapping[str, Any],
        other_index: dict[Any, dict[str, Mapping[str, Any]]],
        left_is_self: bool,
        *,
        sign: int,
    ) -> Delta:
        join_value = row.get(self._lon if left_is_self else self._ron)
        matches = other_index.get(join_value, {})
        delta = ZSet()
        for other in matches.values():
            left, right = (row, other) if left_is_self else (other, row)
            delta.add(Row(self._combine(left, right)), sign)
        return delta

    @staticmethod
    def _index_add(
        index: dict[Any, dict[str, Mapping[str, Any]]],
        value: Any,
        pk_str: str,
        row: Mapping[str, Any],
    ) -> None:
        index.setdefault(value, {})[pk_str] = dict(row)

    @staticmethod
    def _index_remove(
        index: dict[Any, dict[str, Mapping[str, Any]]], value: Any, pk_str: str
    ) -> None:
        bucket = index.get(value)
        if bucket is not None:
            bucket.pop(pk_str, None)
            if not bucket:
                index.pop(value, None)

    # -- consistency oracle ------------------------------------------------- #
    def recompute(self, base: Mapping[str, Iterable[Mapping[str, Any]]]) -> ZSet:
        right_by_val: dict[Any, list[Mapping[str, Any]]] = {}
        for r in base.get(self._rt, []):
            right_by_val.setdefault(r.get(self._ron), []).append(r)
        out = ZSet()
        for left in base.get(self._lt, []):
            for right in right_by_val.get(left.get(self._lon), []):
                out.add(Row(self._combine(left, right)), +1)
        return out


__all__ = ["EquiJoinView"]
