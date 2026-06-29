"""Structural seam onto lakehouse **facet A** (the ``Table`` / ``QueryEngine``).

Facet A (a sibling lakehouse facet) owns a columnar ``Table`` and a
``QueryEngine`` that can scan it. The feature store's offline store wants to read
its historical feature rows from those when they exist, but must stay fully
functional (and unit-testable with no infra) when facet A is absent or still
being built. We therefore depend on facet A *structurally* — a runtime-checkable
:class:`Protocol` — never by import. A concrete ``QueryEngine`` from facet A
satisfies :class:`QueryEngineLike` if it can hand us rows for a logical source;
otherwise the offline store uses its built-in in-memory backend.

The adapter :func:`rows_from_engine` is the one place that knows how to turn a
facet-A scan into :class:`FeatureRow` objects, so the rest of the feature store
never sees facet A's concrete shape.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from .rows import FeatureRow
from .types import FeatureView


@runtime_checkable
class TableLike(Protocol):
    """The minimal columnar-table shape the feature store reads from facet A."""

    @property
    def name(self) -> str: ...

    def iter_rows(self) -> Iterable[Mapping[str, object]]:
        """Yield each row as a column→value mapping."""
        ...


@runtime_checkable
class QueryEngineLike(Protocol):
    """The minimal query-engine shape: scan a named logical source to rows.

    Facet A's real ``QueryEngine`` is expected to expose a richer API; the feature
    store only needs to *scan a source for a column-projection*, which any
    reasonable engine can satisfy. Kept narrow so the coupling stays loose.
    """

    def scan(
        self, source: str, *, columns: Sequence[str] | None = None
    ) -> Iterable[Mapping[str, object]]:
        """Yield rows of ``source`` (optionally projected to ``columns``)."""
        ...


def _coerce_ts(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def row_from_mapping(view: FeatureView, mapping: Mapping[str, object]) -> FeatureRow | None:
    """Build a :class:`FeatureRow` for ``view`` from a raw scan row.

    Returns ``None`` if the row lacks the view's event timestamp or any join key —
    such a row cannot participate in a point-in-time join and is skipped rather
    than corrupting the result.
    """
    ts_field = view.source.timestamp_field
    event_ts = _coerce_ts(mapping.get(ts_field))
    if event_ts is None:
        return None
    keys: dict[str, object] = {}
    for jk in view.join_keys:
        if jk not in mapping or mapping[jk] is None:
            return None
        keys[jk] = mapping[jk]
    created_ts: datetime | None = None
    if view.source.created_field is not None:
        created_ts = _coerce_ts(mapping.get(view.source.created_field))
    values = {f.name: mapping.get(f.name) for f in view.features}
    return FeatureRow(
        keys=keys, values=values, event_timestamp=event_ts, created_timestamp=created_ts
    )


def rows_from_engine(engine: QueryEngineLike, view: FeatureView) -> list[FeatureRow]:
    """Scan ``view``'s source via facet A's engine into :class:`FeatureRow`\\ s."""
    columns = (
        list(view.join_keys)
        + [f.name for f in view.features]
        + [view.source.timestamp_field]
    )
    if view.source.created_field is not None:
        columns.append(view.source.created_field)
    out: list[FeatureRow] = []
    for raw in engine.scan(view.source.name, columns=columns):
        row = row_from_mapping(view, raw)
        if row is not None:
            out.append(row)
    return out


__all__ = [
    "QueryEngineLike",
    "TableLike",
    "row_from_mapping",
    "rows_from_engine",
]
