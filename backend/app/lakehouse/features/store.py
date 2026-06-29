"""``FeatureStore`` — the façade that ties the feature platform together.

This is the single object the rest of Kinora talks to. It owns a
:class:`~app.lakehouse.features.registry.FeatureRegistry`, an offline store, an
online store, and a :class:`~app.lakehouse.features.monitoring.FeatureMonitor`,
and exposes the end-to-end operations:

* **define** — register entities / feature views / on-demand views / services.
* **ingest** (offline) — append historical observations.
* **push** (streaming) — write fresh rows straight to the online store.
* **get_training_data** — point-in-time-correct training set for a feature
  service or an ad-hoc reference list (no label leakage).
* **materialize** — push the latest offline values to the online store.
* **get_online_features** — low-latency current vector for one entity, with
  on-demand features applied.
* **validate_parity** / **detect_skew** / **assess_freshness** — the quality
  checks, each recording into the monitor.

Construction stays infra-free: with no Redis client it uses the in-memory online
store, so the whole platform runs in the hermetic unit suite. Point it at the
app's :class:`~app.redis.client.RedisClient` (and lakehouse facet A's engine) for
production serving.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from .engine_seam import QueryEngineLike
from .freshness import FreshnessReport, freshness_report
from .materialization import MaterializationResult, materialize
from .monitoring import FeatureMonitor
from .offline_store import (
    EngineOfflineStore,
    InMemoryOfflineStore,
    OfflineStore,
    get_historical_features,
)
from .on_demand import apply_on_demand, push_stream_rows
from .online_store import (
    InMemoryOnlineStore,
    OnlineStore,
    RedisOnlineStore,
    get_online_features,
)
from .parity import ParityReport, SkewReport, check_parity, detect_skew
from .registry import FeatureRegistry, OnDemandFn
from .rows import EntityRow, FeatureRow, Frame
from .types import (
    Entity,
    FeatureRef,
    FeatureService,
    FeatureSpec,
    FeatureView,
    OnDemandFeatureView,
    parse_refs,
)


class FeatureStore:
    """The high-level feature platform façade (registry + stores + quality)."""

    def __init__(
        self,
        *,
        registry: FeatureRegistry | None = None,
        offline: OfflineStore | None = None,
        online: OnlineStore | None = None,
        redis_client: Any | None = None,
        engine: QueryEngineLike | None = None,
        monitor: FeatureMonitor | None = None,
    ) -> None:
        self.registry = registry or FeatureRegistry()
        self.monitor = monitor or FeatureMonitor()
        if offline is not None:
            self.offline = offline
        elif engine is not None:
            self.offline = EngineOfflineStore(engine, self.registry)
        else:
            self.offline = InMemoryOfflineStore()
        if online is not None:
            self.online = online
        elif redis_client is not None:
            self.online = RedisOnlineStore(redis_client)
        else:
            self.online = InMemoryOnlineStore()

    # -- definition ------------------------------------------------------ #

    def register_entity(self, entity: Entity) -> Entity:
        return self.registry.register_entity(entity)

    def register_feature_view(self, view: FeatureView) -> FeatureView:
        return self.registry.register_feature_view(view)

    def register_on_demand_view(
        self, view: OnDemandFeatureView, fn: OnDemandFn
    ) -> OnDemandFeatureView:
        return self.registry.register_on_demand_view(view, fn)

    def register_feature_service(self, service: FeatureService) -> FeatureService:
        return self.registry.register_feature_service(service)

    # -- ingestion ------------------------------------------------------- #

    def ingest(self, view: str, rows: Sequence[FeatureRow]) -> int:
        """Append historical observations for a feature view (offline store)."""
        # Validate the view exists before writing.
        self.registry.get_feature_view(view)
        written = self.offline.write(view, rows)
        self.monitor.incr("offline_rows_written", written)
        return written

    async def push(self, view: str, rows: Sequence[FeatureRow]) -> int:
        """Push streaming rows straight to the online store (immediate serving)."""
        fv = self.registry.get_feature_view(view)
        # Also retain in the offline history so training joins see streamed events.
        self.offline.write(view, rows)
        n = await push_stream_rows(fv, rows, online=self.online)
        self.monitor.incr("stream_keys_pushed", n)
        return n

    # -- training -------------------------------------------------------- #

    def get_training_data(
        self,
        *,
        entities: Sequence[EntityRow],
        refs: Sequence[str | FeatureRef] | None = None,
        service: str | None = None,
        full_feature_names: bool = True,
    ) -> Frame:
        """Point-in-time-correct training set (by feature service or ad-hoc refs)."""
        resolved_refs = self._refs_for(refs=refs, service=service)
        frame = get_historical_features(
            self.offline,
            self.registry,
            entities=entities,
            refs=resolved_refs,
            full_feature_names=full_feature_names,
        )
        self.monitor.record_training_set(len(frame))
        return frame

    # -- materialisation ------------------------------------------------- #

    async def materialize(
        self, *, as_of: datetime, views: Sequence[str] | None = None
    ) -> list[MaterializationResult]:
        results = await materialize(
            self.registry, offline=self.offline, online=self.online, as_of=as_of, views=views
        )
        for r in results:
            self.monitor.record_materialization(r)
        return results

    # -- serving --------------------------------------------------------- #

    async def get_online_features(
        self,
        *,
        keys: Mapping[str, object],
        refs: Sequence[str | FeatureRef] | None = None,
        service: str | None = None,
        request: Mapping[str, object] | None = None,
        on_demand_views: Sequence[str] = (),
    ) -> dict[str, object]:
        """Low-latency current feature vector for one entity (+ on-demand features)."""
        resolved_refs = self._refs_for(refs=refs, service=service)
        views = self._views_for(resolved_refs)
        vector = await get_online_features(self.online, views=views, keys=keys)
        for view in views:
            kt = tuple(keys.get(jk) for jk in view.join_keys)
            value = await self.online.get(view, kt)
            self.monitor.record_online_read(hit=value is not None)
        if on_demand_views:
            vector = apply_on_demand(
                self.registry,
                base=vector,
                on_demand_views=on_demand_views,
                request=request or {},
            )
        # Project to exactly the requested ref columns (+ on-demand outputs).
        wanted = [r.column for r in resolved_refs]
        for odv_name in on_demand_views:
            odv = self.registry.get_on_demand_view(odv_name)
            wanted.extend(f"{odv_name}__{f.name}" for f in odv.features)
        return {col: vector.get(col) for col in wanted}

    # -- quality checks -------------------------------------------------- #

    def validate_parity(
        self,
        view: str,
        *,
        offline: Mapping[str, Mapping[str, object]],
        online: Mapping[str, Mapping[str, object]],
        rel_tol: float = 1e-6,
    ) -> ParityReport:
        fv = self.registry.get_feature_view(view)
        report = check_parity(fv.features, offline=offline, online=online, rel_tol=rel_tol)
        self.monitor.record_parity(view, report)
        return report

    def detect_skew(
        self,
        view: str,
        *,
        reference: Mapping[str, Sequence[object]],
        current: Mapping[str, Sequence[object]],
        moderate: float = 0.1,
        large: float = 0.25,
    ) -> SkewReport:
        fv = self.registry.get_feature_view(view)
        report = detect_skew(
            fv.features,
            reference=reference,
            current=current,
            moderate=moderate,
            large=large,
        )
        self.monitor.record_skew(view, report)
        return report

    def assess_freshness(
        self,
        view: str,
        *,
        event_timestamps: Sequence[datetime | None],
        now: datetime,
        sla: timedelta | None = None,
    ) -> FreshnessReport:
        fv = self.registry.get_feature_view(view)
        report = freshness_report(fv, event_timestamps=event_timestamps, now=now, sla=sla)
        self.monitor.record_freshness(report)
        return report

    # -- internals ------------------------------------------------------- #

    def _refs_for(
        self,
        *,
        refs: Sequence[str | FeatureRef] | None,
        service: str | None,
    ) -> tuple[FeatureRef, ...]:
        if service is not None:
            if refs is not None:
                raise ValueError("pass either a feature service or refs, not both")
            return self.registry.get_feature_service(service).refs()
        if refs is None:
            raise ValueError("one of `service` or `refs` is required")
        return parse_refs(refs)

    def _views_for(self, refs: Sequence[FeatureRef]) -> list[FeatureView]:
        seen: dict[tuple[str, int], FeatureView] = {}
        for ref in refs:
            view, _ = self.registry.resolve(ref)
            seen.setdefault((view.name, view.version), view)
        return list(seen.values())

    def feature_specs(self, view: str) -> tuple[FeatureSpec, ...]:
        return self.registry.get_feature_view(view).features


__all__ = ["FeatureStore"]
