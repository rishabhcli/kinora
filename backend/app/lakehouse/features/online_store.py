"""The online (serving) store — low-latency latest-value reads for inference.

At serving time a model needs the *current* value of each feature for a small set
of entity keys, in single-digit milliseconds — not a historical join. The online
store holds exactly one materialised value per (feature view, entity key): the
newest value as of the last materialisation. It is written by the materialiser
(:mod:`materialization`) and read by :meth:`get_online_features`.

Two backends implement the async :class:`OnlineStore` protocol:

* :class:`InMemoryOnlineStore` — a dict-backed store for tests and hermetic runs.
* :class:`RedisOnlineStore` — the production backend over the app's
  :class:`~app.redis.client.RedisClient`. One Redis key per (view-version, key)
  holds the JSON-encoded feature payload + its event timestamp, with the feature
  view's TTL applied as the Redis key expiry so a value physically disappears once
  it is stale (a second, defence-in-depth layer on top of the materialiser's
  TTL-aware pick).

The online value carries its source ``event_timestamp`` so the parity checker and
freshness monitor can compare it against the offline as-of value and against the
clock.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .types import FeatureView


@dataclass(frozen=True, slots=True)
class OnlineValue:
    """A materialised online row: the feature payload + its source event time."""

    values: Mapping[str, object]
    event_timestamp: datetime | None


def _key_str(key_tuple: Sequence[object]) -> str:
    """A stable string for an entity key tuple (used in dict/Redis keys)."""
    return "\x1f".join("" if k is None else str(k) for k in key_tuple)


class OnlineStore(Protocol):
    """The serving-store contract (async; one latest value per key per view)."""

    async def set_many(
        self, view: FeatureView, items: Mapping[tuple[object, ...], OnlineValue]
    ) -> int:
        """Materialise ``items`` for ``view``; return the count written."""
        ...

    async def get(
        self, view: FeatureView, key_tuple: tuple[object, ...]
    ) -> OnlineValue | None:
        """Read the latest value for one entity key (``None`` if absent)."""
        ...


class InMemoryOnlineStore:
    """Deterministic dict-backed online store keyed by (view-version, entity key)."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, int, str], OnlineValue] = {}

    async def set_many(
        self, view: FeatureView, items: Mapping[tuple[object, ...], OnlineValue]
    ) -> int:
        for key_tuple, value in items.items():
            self._data[(view.name, view.version, _key_str(key_tuple))] = value
        return len(items)

    async def get(
        self, view: FeatureView, key_tuple: tuple[object, ...]
    ) -> OnlineValue | None:
        return self._data.get((view.name, view.version, _key_str(key_tuple)))

    async def clear(self) -> None:
        self._data.clear()

    def size(self) -> int:
        return len(self._data)


class RedisOnlineStore:
    """Redis-backed online store over :class:`~app.redis.client.RedisClient`.

    Key layout: ``{namespace}:{view}:{version}:{entity-key}`` → JSON
    ``{"v": {...}, "ts": "<iso>"}``. The feature view's TTL is applied as the Redis
    key expiry (rounded up to whole seconds), so a value evaporates once it is past
    its freshness window even if the materialiser has not run again.
    """

    def __init__(self, client: Any, *, namespace: str = "kinora:featstore:online") -> None:
        self._client = client
        self._ns = namespace

    def _key(self, view: FeatureView, key_tuple: Sequence[object]) -> str:
        return f"{self._ns}:{view.name}:{view.version}:{_key_str(key_tuple)}"

    @staticmethod
    def _ttl_seconds(view: FeatureView) -> int | None:
        if view.ttl is None:
            return None
        return max(1, int(view.ttl.total_seconds()))

    async def set_many(
        self, view: FeatureView, items: Mapping[tuple[object, ...], OnlineValue]
    ) -> int:
        ttl = self._ttl_seconds(view)
        count = 0
        for key_tuple, value in items.items():
            payload = {
                "v": value.values,
                "ts": None if value.event_timestamp is None else value.event_timestamp.isoformat(),
            }
            await self._client.set_json(self._key(view, key_tuple), payload, ttl_s=ttl)
            count += 1
        return count

    async def get(
        self, view: FeatureView, key_tuple: tuple[object, ...]
    ) -> OnlineValue | None:
        raw = await self._client.get_json(self._key(view, key_tuple))
        if raw is None:
            return None
        ts = raw.get("ts")
        event_ts = datetime.fromisoformat(ts) if isinstance(ts, str) else None
        values = raw.get("v") or {}
        return OnlineValue(values=values, event_timestamp=event_ts)

    async def delete(self, view: FeatureView, key_tuple: tuple[object, ...]) -> None:
        await self._client.delete(self._key(view, key_tuple))


async def get_online_features(
    store: OnlineStore,
    *,
    views: Sequence[FeatureView],
    keys: Mapping[str, object],
) -> dict[str, object]:
    """Read the current feature vector for one entity across ``views``.

    Returns a ``view__feature`` → value mapping, filling each feature's declared
    default for any view/key the online store has no (or an expired) value for.
    This is the serving counterpart of :func:`offline_store.get_historical_features`
    — same column naming so a feature service yields the same layout in both paths.
    """
    out: dict[str, object] = {}
    for view in views:
        key_tuple = tuple(keys.get(jk) for jk in view.join_keys)
        value = await store.get(view, key_tuple)
        for spec in view.features:
            col = f"{view.name}__{spec.name}"
            served = None if value is None else value.values.get(spec.name)
            out[col] = served if served is not None else spec.default
    return out


def serialize_value(value: OnlineValue) -> str:
    """JSON-encode an online value (used by tests + diagnostics)."""
    return json.dumps(
        {
            "v": dict(value.values),
            "ts": None if value.event_timestamp is None else value.event_timestamp.isoformat(),
        },
        separators=(",", ":"),
        default=str,
    )


__all__ = [
    "InMemoryOnlineStore",
    "OnlineStore",
    "OnlineValue",
    "RedisOnlineStore",
    "get_online_features",
    "serialize_value",
]
