"""Row + frame value objects shared by the stores and the point-in-time join.

Kept deliberately dependency-free (no pandas) so the feature store runs in the
hermetic unit suite and on the API image without extra wheels. A :class:`Frame`
is a thin, immutable, columnar-ish table of dict rows with a known column order —
just enough to express an entity dataframe and a training set without pulling a
dataframe library. When facet A's columnar :class:`Table` is present, the offline
store consumes it through the :mod:`engine_seam` protocol instead; this is the
fallback representation.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

#: One feature row: the join-key values + a feature payload + an event timestamp.
Row = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class FeatureRow:
    """A single stored observation for a feature view.

    ``keys`` maps each join-key column to its entity-id value; ``values`` maps each
    feature name to its observed value; ``event_timestamp`` is the event time the
    point-in-time join merges on; ``created_timestamp`` (optional) is the arrival
    time used as a tie-breaker for late-arriving duplicates at the same event time.
    """

    keys: Mapping[str, object]
    values: Mapping[str, object]
    event_timestamp: datetime
    created_timestamp: datetime | None = None

    def key_tuple(self, join_keys: Sequence[str]) -> tuple[object, ...]:
        """The entity-key identity tuple in a fixed join-key order."""
        return tuple(self.keys[k] for k in join_keys)


@dataclass(frozen=True, slots=True)
class EntityRow:
    """One request row for a point-in-time / online lookup.

    ``keys`` carries the join-key values; ``event_timestamp`` is the *label time*
    (the moment we are predicting for) — the join returns each feature's value as
    known strictly at or before this instant. ``request`` carries on-demand inputs
    available at request time (the streaming/on-demand seam).
    """

    keys: Mapping[str, object]
    event_timestamp: datetime
    request: Mapping[str, object] = field(default_factory=dict)

    def key_tuple(self, join_keys: Sequence[str]) -> tuple[object, ...]:
        return tuple(self.keys[k] for k in join_keys)


@dataclass(frozen=True, slots=True)
class Frame:
    """An immutable, ordered table of dict rows with a fixed column order.

    A minimal stand-in for a dataframe: it is what :meth:`get_historical_features`
    returns (a training set) and what tests assert over. Column order is stable so
    a feature service yields a deterministic vector layout for training/serving.
    """

    columns: tuple[str, ...]
    rows: tuple[Mapping[str, object], ...]

    @classmethod
    def from_rows(
        cls, rows: Iterable[Mapping[str, object]], *, columns: Sequence[str] | None = None
    ) -> Frame:
        materialized = tuple(dict(r) for r in rows)
        if columns is not None:
            cols = tuple(columns)
        else:
            seen: list[str] = []
            seen_set: set[str] = set()
            for r in materialized:
                for k in r:
                    if k not in seen_set:
                        seen.append(k)
                        seen_set.add(k)
            cols = tuple(seen)
        return cls(columns=cols, rows=materialized)

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Iterator[Mapping[str, object]]:
        return iter(self.rows)

    def column(self, name: str) -> list[object]:
        """The values of one column, in row order (missing → ``None``)."""
        return [r.get(name) for r in self.rows]

    def select(self, columns: Sequence[str]) -> Frame:
        cols = tuple(columns)
        return Frame(
            columns=cols,
            rows=tuple({c: r.get(c) for c in cols} for r in self.rows),
        )

    def to_dicts(self) -> list[dict[str, object]]:
        """A plain list of dict rows (each containing exactly :attr:`columns`)."""
        return [{c: r.get(c) for c in self.columns} for r in self.rows]


__all__ = ["EntityRow", "FeatureRow", "Frame", "Row"]
