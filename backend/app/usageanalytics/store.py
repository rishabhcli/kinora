"""The metric store: where bucketed cost-&-usage cells live.

:class:`UsageMetricStore` is the persistence seam — a small ``Protocol`` the
aggregation engine and dashboard service read/write through. Two implementations
ship:

* :class:`InMemoryUsageMetricStore` — the default. Deterministic, no infra, the
  test backend, and a viable embedded backend for a single-process operator UI.
  It bucket-indexes raw events at the finest granularity and rolls up on read.
* :class:`RedisUsageMetricStore` — a thin adapter against a **redis-interface**
  (any object exposing ``hincrbyfloat`` / ``hgetall`` etc.). Kept import-light and
  infra-free in this module by depending only on a duck-typed client; the live
  wiring passes the real async redis client. Tests use the in-memory store.

A *dimension* is a tuple of optional ``(provider, model, book_id, session_id)``
selectors; the store keys cells on the full tuple and the engine slices by
projecting onto the requested group-by axes. ``None`` in a slot means "all".
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import Protocol, runtime_checkable

from app.usageanalytics.events import MetricCell, Provider, UsageEvent
from app.usageanalytics.window import (
    Granularity,
    RetentionPolicy,
    downsample_buckets,
)


@dataclass(frozen=True, slots=True)
class Dimension:
    """A fully-qualified slice key. ``None`` slots are "unset" at storage time.

    At storage time every slot is concrete (the event's actual values). At query
    time a :class:`Dimension` is a *filter*: a ``None`` slot matches any value.
    """

    provider: Provider | None = None
    model: str | None = None
    book_id: str | None = None
    session_id: str | None = None

    @classmethod
    def of(cls, ev: UsageEvent) -> Dimension:
        """The concrete dimension an event is filed under."""
        return cls(
            provider=ev.provider,
            model=ev.model,
            book_id=ev.book_id,
            session_id=ev.session_id,
        )

    def matches(self, other: Dimension) -> bool:
        """Does the *concrete* ``other`` satisfy this (filter) dimension?"""
        return (
            (self.provider is None or self.provider == other.provider)
            and (self.model is None or self.model == other.model)
            and (self.book_id is None or self.book_id == other.book_id)
            and (self.session_id is None or self.session_id == other.session_id)
        )


class Axis(str):
    """A group-by axis name (``provider`` / ``model`` / ``book`` / ``session``)."""


#: The four group-by axes a dashboard slices on.
PROVIDER: Axis = Axis("provider")
MODEL: Axis = Axis("model")
BOOK: Axis = Axis("book")
SESSION: Axis = Axis("session")


def project(dim: Dimension, axes: Sequence[str]) -> tuple[str, ...]:
    """Project a concrete dimension onto the requested axes (the group key).

    Unknown axes are ignored. A missing value renders as ``""`` so the key is
    always a stable string tuple.
    """
    out: list[str] = []
    for axis in axes:
        if axis == PROVIDER:
            out.append(str(dim.provider) if dim.provider is not None else "")
        elif axis == MODEL:
            out.append(dim.model or "")
        elif axis == BOOK:
            out.append(dim.book_id or "")
        elif axis == SESSION:
            out.append(dim.session_id or "")
    return tuple(out)


@runtime_checkable
class UsageMetricStore(Protocol):
    """The persistence seam every roll-up reads/writes through."""

    def record(self, ev: UsageEvent) -> None:
        """File one event into its finest-grain bucket + dimension cell."""
        ...

    def record_many(self, events: Iterable[UsageEvent]) -> int:
        """File a batch; return the count recorded."""
        ...

    def cells(
        self, granularity: Granularity, since: datetime, until: datetime, where: Dimension
    ) -> dict[datetime, list[tuple[Dimension, MetricCell]]]:
        """Bucketed cells in ``[since, until)`` at ``granularity``, filtered by ``where``.

        Returns ``{bucket_start: [(concrete_dimension, cell), ...]}``. The engine
        groups/totals these; the store only buckets and filters.
        """
        ...

    def prune(self, now: datetime, policy: RetentionPolicy) -> int:
        """Apply the retention policy; return the number of raw events dropped."""
        ...

    def event_count(self) -> int:
        """Total raw events currently retained (diagnostics)."""
        ...


class InMemoryUsageMetricStore:
    """A deterministic, infra-free :class:`UsageMetricStore`.

    Holds the raw events (timestamp-ordered per dimension) and rolls them up into
    bucketed cells on demand. Thread-safe via a single lock — writes are cheap
    appends, reads are pure folds. Retention drops raw events older than the
    coarsest tier; finer-than-tier data is implicitly coarsened on read because
    the engine asks for the coarse granularity.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        # dimension -> time-sorted list of events under it.
        self._by_dim: dict[Dimension, list[UsageEvent]] = defaultdict(list)

    def record(self, ev: UsageEvent) -> None:
        with self._lock:
            self._by_dim[Dimension.of(ev)].append(ev)

    def record_many(self, events: Iterable[UsageEvent]) -> int:
        n = 0
        with self._lock:
            for ev in events:
                self._by_dim[Dimension.of(ev)].append(ev)
                n += 1
        return n

    def cells(
        self, granularity: Granularity, since: datetime, until: datetime, where: Dimension
    ) -> dict[datetime, list[tuple[Dimension, MetricCell]]]:
        out: dict[datetime, dict[Dimension, MetricCell]] = defaultdict(dict)
        with self._lock:
            items = list(self._by_dim.items())
        for dim, events in items:
            if not where.matches(dim):
                continue
            for ev in events:
                if ev.at < since or ev.at >= until:
                    continue
                bucket = granularity.floor(ev.at)
                cell = out[bucket].get(dim)
                if cell is None:
                    cell = MetricCell()
                    out[bucket][dim] = cell
                cell.add(ev)
        return {b: list(d.items()) for b, d in sorted(out.items())}

    def prune(self, now: datetime, policy: RetentionPolicy) -> int:
        cutoff = now - policy.horizon
        dropped = 0
        with self._lock:
            for dim, events in list(self._by_dim.items()):
                kept = [e for e in events if e.at >= cutoff]
                dropped += len(events) - len(kept)
                if kept:
                    self._by_dim[dim] = kept
                else:
                    del self._by_dim[dim]
        return dropped

    def event_count(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._by_dim.values())

    # --- helpers the engine/tests use ------------------------------------- #

    def rollup_series(
        self,
        granularity: Granularity,
        since: datetime,
        until: datetime,
        where: Dimension,
        downsample_to: Granularity | None = None,
    ) -> dict[datetime, MetricCell]:
        """Total all dimensions per bucket, optionally downsampled to a coarser grain."""
        raw = self.cells(granularity, since, until, where)
        merged: dict[datetime, MetricCell] = {}
        for bucket, pairs in raw.items():
            agg = MetricCell()
            for _dim, cell in pairs:
                agg.merge(cell)
            merged[bucket] = agg
        if downsample_to is not None and downsample_to.is_coarser_than(granularity):
            merged = downsample_buckets(merged, downsample_to)
        return merged


class RedisUsageMetricStore:
    """A redis-interface adapter (duck-typed client; no hard redis import).

    The live wiring injects the real async redis client; this class exists to
    document the seam and keep the in-memory store the single source of truth for
    tests. Methods that would block on I/O raise :class:`NotImplementedError`
    here so a misconfiguration fails loudly rather than silently no-ops. (The
    deterministic suite uses :class:`InMemoryUsageMetricStore`.)
    """

    #: Redis key prefix for the bucketed hashes.
    KEY_PREFIX = "kinora:ua"

    def __init__(self, client: object, ttl: timedelta | None = None) -> None:
        self._client = client
        self._ttl = ttl

    @staticmethod
    def bucket_key(granularity: Granularity, bucket: datetime, dim: Dimension) -> str:
        """The redis hash key for one (granularity, bucket, dimension) cell."""
        stamp = bucket.strftime("%Y%m%dT%H%M%S")
        slot = "|".join(
            (
                str(dim.provider or ""),
                dim.model or "",
                dim.book_id or "",
                dim.session_id or "",
            )
        )
        return f"{RedisUsageMetricStore.KEY_PREFIX}:{granularity}:{stamp}:{slot}"

    def record(self, ev: UsageEvent) -> None:  # pragma: no cover - infra adapter
        raise NotImplementedError("RedisUsageMetricStore requires a live async client")

    def record_many(self, events: Iterable[UsageEvent]) -> int:  # pragma: no cover
        raise NotImplementedError("RedisUsageMetricStore requires a live async client")

    def cells(  # pragma: no cover - infra adapter
        self, granularity: Granularity, since: datetime, until: datetime, where: Dimension
    ) -> dict[datetime, list[tuple[Dimension, MetricCell]]]:
        raise NotImplementedError("RedisUsageMetricStore requires a live async client")

    def prune(self, now: datetime, policy: RetentionPolicy) -> int:  # pragma: no cover
        raise NotImplementedError("RedisUsageMetricStore requires a live async client")

    def event_count(self) -> int:  # pragma: no cover - infra adapter
        raise NotImplementedError("RedisUsageMetricStore requires a live async client")


__all__ = [
    "BOOK",
    "MODEL",
    "PROVIDER",
    "SESSION",
    "Axis",
    "Dimension",
    "InMemoryUsageMetricStore",
    "RedisUsageMetricStore",
    "UsageMetricStore",
    "project",
]
