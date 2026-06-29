"""Point-in-time-correct feature joins — the anti-label-leakage core.

The single most important guarantee a feature store makes for *training* is this:
when you build a training set for a label observed at time ``r``, every feature
value joined onto that row must be a value that was **already known at or before
``r``**. Joining a feature observed *after* ``r`` leaks the future into the
training set and silently inflates offline metrics — the model looks great in the
notebook and collapses in production. This module makes that leak structurally
impossible.

The join is a **backward as-of merge per entity key**:

For an entity request ``(keys, r)`` and a feature view with join keys ``K``, TTL
``ttl`` (possibly ``None``), and stored rows sharing ``keys``:

    pick the row with the greatest ``event_timestamp t`` such that  t <= r
    and (ttl is None or t > r - ttl);
    tie-break equal ``t`` by the greatest ``created_timestamp`` (latest arrival
    of a value that was true at the same instant), then by stored order.

If no row qualifies, every feature in the view falls back to its declared default
(or ``None``). The asymmetric window ``(r - ttl, r]`` is what enforces both
**recency** (no stale value past its TTL — §8.5 forgetting) and **causality** (no
value from the future — no leakage).

Everything here is pure and deterministic: same inputs → same output, no clock,
no I/O. That is what lets us property-test the correctness invariants with
Hypothesis (``tests/lakehouse/features/test_pit_properties.py``):

* **No leakage:** no joined value ever has ``t > r``.
* **As-of maximality:** the joined value is the newest eligible one.
* **TTL bound:** no joined value has ``t <= r - ttl``.
* **Monotone request time:** moving ``r`` forward never *removes* an already
  eligible value (it can only reveal newer ones).
* **Determinism / order-independence:** shuffling the stored rows does not change
  the result.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from .rows import EntityRow, FeatureRow, Frame
from .types import FeatureView, PointInTimeError


@dataclass(frozen=True, slots=True)
class JoinedFeature:
    """The result of resolving one feature view for one entity request."""

    #: Resolved feature values (name → value), defaults filled for misses.
    values: Mapping[str, object]
    #: The event timestamp of the chosen source row (``None`` if a default was used).
    as_of: datetime | None
    #: Whether a stored row was found within the TTL window (vs. a default fill).
    hit: bool


def _eligible(
    row: FeatureRow, *, request_ts: datetime, ttl: timedelta | None
) -> bool:
    """Whether ``row`` is a causally-valid, non-stale candidate for ``request_ts``."""
    t = row.event_timestamp
    if t > request_ts:
        return False  # future value → would leak the label's future
    # Reject anything at/older than the freshness window (stale); else eligible.
    return not (ttl is not None and t <= request_ts - ttl)


def _payload_key(row: FeatureRow) -> str:
    """A stable, total-order string for a row's payload (the final tie-break).

    When two rows share both event time *and* arrival time, the as-of pick would
    otherwise depend on input order — a determinism bug a property test surfaces.
    Breaking the final tie on a canonical serialisation of the feature payload
    makes the result independent of how the rows were stored/shuffled.
    """
    return json.dumps(
        sorted((str(k), repr(v)) for k, v in row.values.items()),
        separators=(",", ":"),
    )


def _better(candidate: FeatureRow, incumbent: FeatureRow) -> bool:
    """True if ``candidate`` should win the as-of pick over ``incumbent``.

    Total ordering, so the pick is deterministic regardless of input order:
    newer event time wins; ties broken by newer arrival (``created_timestamp``,
    a missing arrival treated as the oldest possible so an explicit arrival always
    beats an unknown one); remaining ties broken by a canonical payload string.
    """
    if candidate.event_timestamp != incumbent.event_timestamp:
        return candidate.event_timestamp > incumbent.event_timestamp
    c_created = candidate.created_timestamp or datetime.min.replace(
        tzinfo=candidate.event_timestamp.tzinfo
    )
    i_created = incumbent.created_timestamp or datetime.min.replace(
        tzinfo=incumbent.event_timestamp.tzinfo
    )
    if c_created != i_created:
        return c_created > i_created
    # Fully tied on both timestamps → deterministic payload tie-break.
    return _payload_key(candidate) > _payload_key(incumbent)


def point_in_time_lookup(
    view: FeatureView,
    *,
    keys: Mapping[str, object],
    request_ts: datetime,
    rows: Sequence[FeatureRow],
) -> JoinedFeature:
    """Resolve one feature view for one entity at ``request_ts`` (the as-of pick).

    ``rows`` may contain rows for *any* key; this filters to ``keys`` first. The
    pick is O(n) over the matching rows — order-independent, so callers need not
    pre-sort. Defaults fill every feature when nothing qualifies.
    """
    join_keys = view.join_keys
    try:
        wanted = tuple(keys[k] for k in join_keys)
    except KeyError as exc:  # pragma: no cover - defensive
        raise PointInTimeError(
            f"entity row is missing join key {exc.args[0]!r} for view {view.name!r}"
        ) from exc

    best: FeatureRow | None = None
    for row in rows:
        if row.key_tuple(join_keys) != wanted:
            continue
        if not _eligible(row, request_ts=request_ts, ttl=view.ttl):
            continue
        if best is None or _better(row, best):
            best = row

    if best is None:
        return JoinedFeature(
            values={f.name: f.default for f in view.features},
            as_of=None,
            hit=False,
        )

    resolved: dict[str, object] = {}
    for spec in view.features:
        if spec.name in best.values and best.values[spec.name] is not None:
            resolved[spec.name] = best.values[spec.name]
        else:
            resolved[spec.name] = spec.default
    return JoinedFeature(values=resolved, as_of=best.event_timestamp, hit=True)


def point_in_time_join(
    entities: Sequence[EntityRow],
    *,
    views: Sequence[FeatureView],
    sources: Mapping[str, Sequence[FeatureRow]],
    full_feature_names: bool = True,
    include_event_timestamp: bool = True,
) -> Frame:
    """Build a training set: as-of-join every ``view`` onto every entity row.

    For each entity request ``(keys, r)`` and each feature view, the newest
    causally-valid value within the view's TTL window is selected (see module
    docstring). The result is a :class:`Frame` with one row per entity request, in
    input order, columns = join keys (+ event_timestamp) + one column per
    (view, feature). With ``full_feature_names`` the feature columns are namespaced
    ``view__feature`` to avoid collisions across views; otherwise the bare feature
    name is used (and a collision raises).

    ``sources`` maps each view name to its candidate rows. A view absent from
    ``sources`` contributes only defaults (a freshly-registered view with no data).
    """
    # Stable union of join-key columns across all views (in first-seen order).
    key_columns: list[str] = []
    seen_keys: set[str] = set()
    for view in views:
        for jk in view.join_keys:
            if jk not in seen_keys:
                key_columns.append(jk)
                seen_keys.add(jk)

    # Resolve the output column for each (view, feature) and guard collisions.
    feature_columns: list[tuple[FeatureView, str, str]] = []
    used: set[str] = set(key_columns)
    if include_event_timestamp:
        used.add("event_timestamp")
    for view in views:
        for spec in view.features:
            col = f"{view.name}__{spec.name}" if full_feature_names else spec.name
            if col in used:
                raise PointInTimeError(
                    f"output column {col!r} collides; "
                    "use full_feature_names=True to namespace by view"
                )
            used.add(col)
            feature_columns.append((view, spec.name, col))

    columns: list[str] = list(key_columns)
    if include_event_timestamp:
        columns.append("event_timestamp")
    columns.extend(col for _, _, col in feature_columns)

    out_rows: list[dict[str, object]] = []
    for ent in entities:
        record: dict[str, object] = {}
        for jk in key_columns:
            record[jk] = ent.keys.get(jk)
        if include_event_timestamp:
            record["event_timestamp"] = ent.event_timestamp
        # One lookup per view (cached so multiple features of a view share a pick).
        for view in views:
            joined = point_in_time_lookup(
                view,
                keys=ent.keys,
                request_ts=ent.event_timestamp,
                rows=sources.get(view.name, ()),
            )
            for v, feat_name, col in feature_columns:
                if v is view:
                    record[col] = joined.values.get(feat_name)
        out_rows.append(record)

    return Frame(columns=tuple(columns), rows=tuple(out_rows))


__all__ = [
    "JoinedFeature",
    "point_in_time_join",
    "point_in_time_lookup",
]
