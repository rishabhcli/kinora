"""The offline (historical) store — the source of truth for training sets.

The offline store holds the full append-only history of feature observations and
serves them to two consumers:

* :meth:`get_historical_features` — the **training-set generator**. Given an
  entity dataframe (one row per label, each with a label time) and a set of
  feature references, it runs the point-in-time join (:mod:`pit`) so every joined
  value was known at-or-before its row's label time. No label leakage.
* :meth:`latest_rows` — the **materialisation source**. The newest row per entity
  key as of a reference time, which the materialiser pushes to the online store.

Two backends implement the :class:`OfflineStore` protocol:

* :class:`InMemoryOfflineStore` — a deterministic dict-of-lists store for tests
  and small/hermetic deployments. Appends are idempotent on
  ``(keys, event_timestamp, created_timestamp)``.
* :class:`EngineOfflineStore` — reads each view's rows from lakehouse **facet A**
  (the ``QueryEngine``) when it is present, so the warehouse is the source of
  truth in production. Falls back to the in-memory store for any view facet A
  cannot serve.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from .engine_seam import QueryEngineLike, rows_from_engine
from .pit import point_in_time_join, point_in_time_lookup
from .registry import FeatureRegistry
from .rows import EntityRow, FeatureRow, Frame
from .types import FeatureRef, FeatureView


class OfflineStore(Protocol):
    """The historical-store contract the training-set generator depends on."""

    def write(self, view: str, rows: Sequence[FeatureRow]) -> int:
        """Append observations for ``view``; return the number actually stored."""
        ...

    def source_rows(self, view: FeatureView) -> list[FeatureRow]:
        """All stored rows for ``view`` (the point-in-time join's candidate set)."""
        ...


class InMemoryOfflineStore:
    """Deterministic in-memory offline store (idempotent appends)."""

    def __init__(self) -> None:
        self._rows: dict[str, list[FeatureRow]] = {}
        self._seen: dict[str, set[tuple[object, ...]]] = {}

    def write(self, view: str, rows: Sequence[FeatureRow]) -> int:
        bucket = self._rows.setdefault(view, [])
        seen = self._seen.setdefault(view, set())
        added = 0
        for row in rows:
            ident = (
                tuple(sorted(row.keys.items())),
                row.event_timestamp,
                row.created_timestamp,
            )
            if ident in seen:
                continue
            seen.add(ident)
            bucket.append(row)
            added += 1
        return added

    def source_rows(self, view: FeatureView) -> list[FeatureRow]:
        return list(self._rows.get(view.name, ()))

    def row_count(self, view: str) -> int:
        return len(self._rows.get(view, ()))


class EngineOfflineStore:
    """Offline store backed by lakehouse facet A's :class:`QueryEngineLike`.

    Reads each view's rows from the engine; writes still land in a local in-memory
    overlay (facet A owns ingestion of the warehouse proper — the feature store
    does not write into facet A's tables). For a view the engine cannot scan, this
    transparently uses the overlay, so a registry with both engine-backed and
    locally-written views works uniformly.
    """

    def __init__(self, engine: QueryEngineLike, registry: FeatureRegistry) -> None:
        self._engine = engine
        self._registry = registry
        self._overlay = InMemoryOfflineStore()

    def write(self, view: str, rows: Sequence[FeatureRow]) -> int:
        return self._overlay.write(view, rows)

    def source_rows(self, view: FeatureView) -> list[FeatureRow]:
        rows = list(self._overlay.source_rows(view))
        # facet A may not serve this source (still being built, or offline-only view).
        with contextlib.suppress(Exception):
            rows.extend(rows_from_engine(self._engine, view))
        return rows


def get_historical_features(
    store: OfflineStore,
    registry: FeatureRegistry,
    *,
    entities: Sequence[EntityRow],
    refs: Sequence[str | FeatureRef],
    full_feature_names: bool = True,
) -> Frame:
    """Generate a point-in-time-correct training set for ``entities`` × ``refs``.

    Resolves the references through the registry, gathers each needed view's
    source rows from the offline store, runs the point-in-time join, and projects
    the result to the join keys + ``event_timestamp`` + exactly the referenced
    feature columns (in reference order). The projection matters: a feature service
    pins a *column order*, and training/serving must agree on it.
    """
    views, parsed = registry.views_for_refs(refs)
    sources = {v.name: store.source_rows(v) for v in views}
    full = point_in_time_join(
        entities,
        views=views,
        sources=sources,
        full_feature_names=full_feature_names,
        include_event_timestamp=True,
    )
    # Project to: join keys, event_timestamp, then one column per ref (in order).
    key_cols: list[str] = []
    seen: set[str] = set()
    for v in views:
        for jk in v.join_keys:
            if jk not in seen:
                key_cols.append(jk)
                seen.add(jk)
    feature_cols: list[str] = []
    for ref in parsed:
        view = registry.get_feature_view(ref.view, version=ref.version)
        col = f"{view.name}__{ref.feature}" if full_feature_names else ref.feature
        if col not in feature_cols:
            feature_cols.append(col)
    return full.select([*key_cols, "event_timestamp", *feature_cols])


def latest_rows(
    store: OfflineStore,
    view: FeatureView,
    *,
    as_of: datetime,
) -> dict[tuple[object, ...], FeatureRow]:
    """The newest causally-valid row per entity key as of ``as_of`` (for materialisation).

    Reuses the same TTL-aware as-of pick as the training join so the online store
    is materialised from exactly the value the offline join would have produced —
    this is the structural guarantee behind offline/online parity.
    """
    rows = store.source_rows(view)
    join_keys = view.join_keys
    distinct: dict[tuple[object, ...], Mapping[str, object]] = {}
    for row in rows:
        distinct.setdefault(row.key_tuple(join_keys), row.keys)
    out: dict[tuple[object, ...], FeatureRow] = {}
    for key_tuple, keys in distinct.items():
        joined = point_in_time_lookup(view, keys=keys, request_ts=as_of, rows=rows)
        if joined.hit and joined.as_of is not None:
            out[key_tuple] = FeatureRow(
                keys=dict(keys), values=joined.values, event_timestamp=joined.as_of
            )
    return out


__all__ = [
    "EngineOfflineStore",
    "InMemoryOfflineStore",
    "OfflineStore",
    "get_historical_features",
    "latest_rows",
]
