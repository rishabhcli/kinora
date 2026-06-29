"""Async DB repositories + a DB-backed offline store for the feature platform.

Following the Kinora repository convention (``BaseRepository``: flush, never
commit — the unit of work owns the transaction), these wrap the
``feature_store_*`` tables:

* :class:`FeatureOfflineRepo` — append (idempotent) + bounded read of offline
  feature rows for a view/entity slice. The read mirrors the recommendations
  warehouse's "recall the relevant slice, never the whole log" discipline (§8.4):
  you load the rows for the *views and entity keys you are about to join*, not the
  entire history.
* :class:`FeatureViewDefRepo` — upsert + load the content-addressed registry
  snapshot, so a registry can be persisted and rehydrated.
* :class:`FeatureMaterializationRepo` — record a materialisation run for the
  freshness/lineage monitor.

:class:`DbOfflineStore` adapts the durable rows to the (sync) ``OfflineStore``
protocol the point-in-time join consumes: you :meth:`hydrate` it once for the
views/keys you need (an async DB read), then it serves :meth:`source_rows`
synchronously — the same up-front-load pattern the recommendations service uses.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.base import BaseRepository

from .db_models import FeatureMaterialization, FeatureOfflineRow, FeatureViewDef
from .materialization import MaterializationResult
from .offline_store import InMemoryOfflineStore
from .registry import FeatureRegistry
from .rows import FeatureRow
from .serde import feature_view_from_dict, feature_view_to_dict
from .types import FeatureView


def entity_key_str(keys: Mapping[str, object], join_keys: Sequence[str]) -> str:
    """A stable string for an entity key tuple in a fixed join-key order."""
    return "\x1f".join("" if keys.get(jk) is None else str(keys.get(jk)) for jk in join_keys)


class FeatureOfflineRepo(BaseRepository):
    """Append + bounded read over ``feature_store_offline_rows``."""

    async def append(self, view: FeatureView, rows: Sequence[FeatureRow]) -> int:
        """Idempotently append offline observations for ``view``; return rows added."""
        if not rows:
            return 0
        join_keys = view.join_keys
        values = [
            {
                "view_name": view.name,
                "entity_key": entity_key_str(row.keys, join_keys),
                "keys": dict(row.keys),
                "payload": dict(row.values),
                "event_timestamp": row.event_timestamp,
                "created_timestamp": row.created_timestamp,
            }
            for row in rows
        ]
        stmt = pg_insert(FeatureOfflineRow).values(values)
        # Idempotent on the natural identity (view, key, event_ts, created_ts).
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[
                FeatureOfflineRow.view_name,
                FeatureOfflineRow.entity_key,
                FeatureOfflineRow.event_timestamp,
                FeatureOfflineRow.created_timestamp,
            ]
        )
        result: CursorResult[object] = await self.session.execute(stmt)  # type: ignore[assignment]
        await self.session.flush()
        return int(result.rowcount or 0)

    async def load(
        self,
        view: FeatureView,
        *,
        entity_keys: Sequence[str] | None = None,
        limit: int = 100_000,
    ) -> list[FeatureRow]:
        """Load offline rows for ``view`` (optionally restricted to entity keys).

        Restricting to the entity keys you are about to join keeps the read a
        bounded slice rather than the whole view history.
        """
        stmt = select(FeatureOfflineRow).where(FeatureOfflineRow.view_name == view.name)
        if entity_keys is not None:
            stmt = stmt.where(FeatureOfflineRow.entity_key.in_(list(entity_keys)))
        stmt = stmt.order_by(FeatureOfflineRow.event_timestamp.desc()).limit(limit)
        rows = (await self.session.execute(stmt)).scalars().all()
        return [
            FeatureRow(
                keys=dict(r.keys),
                values=dict(r.payload),
                event_timestamp=r.event_timestamp,
                created_timestamp=r.created_timestamp,
            )
            for r in rows
        ]


class FeatureViewDefRepo(BaseRepository):
    """Upsert + load the content-addressed registry snapshot."""

    async def upsert(self, view: FeatureView) -> None:
        values = {
            "view_name": view.name,
            "version": view.version,
            "definition": feature_view_to_dict(view),
        }
        stmt = pg_insert(FeatureViewDef).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[FeatureViewDef.view_name, FeatureViewDef.version],
            set_={"definition": values["definition"]},
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def load_all(self) -> list[FeatureView]:
        rows = (await self.session.execute(select(FeatureViewDef))).scalars().all()
        return [feature_view_from_dict(r.definition) for r in rows]

    async def rehydrate_into(self, registry: FeatureRegistry) -> int:
        """Register every persisted feature view into ``registry``; return count."""
        views = await self.load_all()
        for view in views:
            registry.register_feature_view(view)
        return len(views)


class FeatureMaterializationRepo(BaseRepository):
    """Append-only materialisation run log."""

    async def record(self, result: MaterializationResult) -> None:
        self.session.add(
            FeatureMaterialization(
                view_name=result.view,
                version=result.version,
                as_of=result.as_of,
                rows_written=result.rows_written,
                keys_total=result.keys_total,
                coverage=result.coverage,
            )
        )
        await self.session.flush()


class DbOfflineStore:
    """A durable offline store satisfying the (sync) ``OfflineStore`` protocol.

    :meth:`hydrate` is an async pre-load (one DB read per view, optionally scoped to
    entity keys); after it, :meth:`source_rows` serves synchronously from the
    in-memory overlay so the point-in-time join — which is sync and pure — can run
    unchanged. :meth:`write` persists new observations through the repo (the caller
    commits via the unit of work) and mirrors them into the overlay.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._repo = FeatureOfflineRepo(session)
        self._overlay = InMemoryOfflineStore()
        self._view_by_name: dict[str, FeatureView] = {}

    async def hydrate(
        self, views: Sequence[FeatureView], *, entity_keys: Sequence[str] | None = None
    ) -> None:
        for view in views:
            self._view_by_name[view.name] = view
            rows = await self._repo.load(view, entity_keys=entity_keys)
            self._overlay.write(view.name, rows)

    async def persist(self, view: FeatureView, rows: Sequence[FeatureRow]) -> int:
        self._view_by_name[view.name] = view
        added = await self._repo.append(view, rows)
        self._overlay.write(view.name, rows)
        return added

    # -- OfflineStore protocol ------------------------------------------- #

    def write(self, view: str, rows: Sequence[FeatureRow]) -> int:
        return self._overlay.write(view, rows)

    def source_rows(self, view: FeatureView) -> list[FeatureRow]:
        return self._overlay.source_rows(view)


__all__ = [
    "DbOfflineStore",
    "FeatureMaterializationRepo",
    "FeatureOfflineRepo",
    "FeatureViewDefRepo",
    "entity_key_str",
]
