"""Materialisation — push the latest offline values into the online store.

Materialisation is the bridge between the two stores: it computes, for each
entity key in a feature view, the newest value as of a reference time (using the
*same* TTL-aware as-of pick as the training join, :func:`offline_store.latest_rows`)
and writes it to the online store. Because both stores are populated from the
identical selection rule, offline/online parity is a structural property rather
than a hope — the parity checker (:mod:`parity`) then verifies it empirically.

A :class:`MaterializationJob` records what was materialised (view, version, as-of,
row count, window) for lineage and freshness monitoring. The job is idempotent:
re-running for the same ``as_of`` overwrites the same online keys with the same
values.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from .offline_store import OfflineStore, latest_rows
from .online_store import OnlineStore, OnlineValue
from .registry import FeatureRegistry
from .types import FeatureView


@dataclass(frozen=True, slots=True)
class MaterializationResult:
    """The outcome of materialising one feature view as of one instant."""

    view: str
    version: int
    as_of: datetime
    rows_written: int
    keys_total: int

    @property
    def coverage(self) -> float:
        """Fraction of distinct entity keys that had a non-stale value."""
        if self.keys_total == 0:
            return 1.0
        return self.rows_written / self.keys_total


async def materialize_view(
    view: FeatureView,
    *,
    offline: OfflineStore,
    online: OnlineStore,
    as_of: datetime,
) -> MaterializationResult:
    """Materialise one feature view's latest values into the online store."""
    latest = latest_rows(offline, view, as_of=as_of)
    items = {
        key_tuple: OnlineValue(values=row.values, event_timestamp=row.event_timestamp)
        for key_tuple, row in latest.items()
    }
    written = await online.set_many(view, items) if items else 0
    # Distinct keys present in the source (denominator for coverage).
    join_keys = view.join_keys
    distinct = {r.key_tuple(join_keys) for r in offline.source_rows(view)}
    return MaterializationResult(
        view=view.name,
        version=view.version,
        as_of=as_of,
        rows_written=written,
        keys_total=len(distinct),
    )


async def materialize(
    registry: FeatureRegistry,
    *,
    offline: OfflineStore,
    online: OnlineStore,
    as_of: datetime,
    views: Sequence[str] | None = None,
) -> list[MaterializationResult]:
    """Materialise some (or all online-enabled) feature views as of ``as_of``.

    With ``views=None`` every registered view whose ``online`` flag is set is
    materialised. Views marked ``online=False`` are offline-only (training-set
    features that are never served) and are skipped.
    """
    targets: list[FeatureView]
    if views is None:
        targets = [v for v in registry.list_feature_views() if v.online]
    else:
        targets = [registry.get_feature_view(name) for name in views]
    results: list[MaterializationResult] = []
    for view in targets:
        results.append(
            await materialize_view(view, offline=offline, online=online, as_of=as_of)
        )
    return results


__all__ = ["MaterializationResult", "materialize", "materialize_view"]
