"""On-demand / streaming feature computation — the request-time transform seam.

Some features cannot be precomputed because they depend on request data: the
canonical example is ``days_since_last_read``, which needs the *request* ``now``
and a stored ``last_read_ts``. These are computed at request time by an on-demand
feature view's transform, fed by request inputs + the values of its source
(stored) views for the same row. This module applies those transforms on top of a
base feature mapping, and provides the **streaming push** seam: a stream/push
source writes fresh rows straight into the online store (and optionally the
offline history) without a batch materialisation, so a just-emitted event is
servable immediately.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from .online_store import OnlineStore, OnlineValue
from .registry import FeatureRegistry
from .rows import FeatureRow
from .types import FeatureView


def apply_on_demand(
    registry: FeatureRegistry,
    *,
    base: Mapping[str, object],
    on_demand_views: Sequence[str],
    request: Mapping[str, object],
) -> dict[str, object]:
    """Augment a base ``view__feature`` mapping with on-demand-computed features.

    For each on-demand view, the upstream feature values it declares as
    ``source_views`` are pulled from ``base`` (by their ``view__feature`` columns)
    and, together with ``request``, fed to the view's transform. Each emitted
    feature is written under ``{odv.name}__{feature}``. The base mapping is not
    mutated; a merged copy is returned.
    """
    out = dict(base)
    for name in on_demand_views:
        view = registry.get_on_demand_view(name)
        upstream: dict[str, object] = {}
        for src_view in view.source_views:
            src = registry.get_feature_view(src_view)
            for spec in src.features:
                col = f"{src_view}__{spec.name}"
                if col in out:
                    # Expose both the namespaced and bare name to the transform.
                    upstream[col] = out[col]
                    upstream.setdefault(spec.name, out[col])
        computed = registry.evaluate_on_demand(name, request=request, upstream=upstream)
        for feat, value in computed.items():
            out[f"{name}__{feat}"] = value
    return out


async def push_stream_rows(
    view: FeatureView,
    rows: Sequence[FeatureRow],
    *,
    online: OnlineStore,
) -> int:
    """Push streaming rows straight into the online store (no batch materialisation).

    For each entity key in ``rows`` the *latest* event-time row wins (a micro-batch
    may carry several events for one key). Used by a push/stream source so a
    just-emitted event is immediately servable. Returns the number of keys written.
    """
    join_keys = view.join_keys
    latest: dict[tuple[object, ...], FeatureRow] = {}
    for row in rows:
        kt = row.key_tuple(join_keys)
        cur = latest.get(kt)
        if cur is None or row.event_timestamp > cur.event_timestamp:
            latest[kt] = row
    items = {
        kt: OnlineValue(values=r.values, event_timestamp=r.event_timestamp)
        for kt, r in latest.items()
    }
    return await online.set_many(view, items) if items else 0


def days_since(timestamp: object, *, now: datetime) -> float | None:
    """A reusable on-demand helper: days between a stored timestamp and ``now``.

    Tolerates ``None`` and ISO strings (the shape an online JSON payload carries).
    """
    if timestamp is None:
        return None
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp)
        except ValueError:
            return None
    if not isinstance(timestamp, datetime):
        return None
    return max(0.0, (now - timestamp).total_seconds() / 86_400.0)


__all__ = ["apply_on_demand", "days_since", "push_stream_rows"]
