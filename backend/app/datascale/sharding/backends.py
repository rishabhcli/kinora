"""Production adapters: bind the abstract sharding seams to real shard engines.

The router, planner, proxy, transaction coordinator and resharding job are all
written against *protocols* so they can be proven with fakes. This module
supplies the concrete implementations that turn a :class:`~app.db.engine.Shard`
URL into live SQLAlchemy engines, connections, and a per-shard
:class:`~app.db.routing.RoutingSessionFactory` — composing the single-cluster
:mod:`app.db` primitives one shard at a time.

* :class:`ShardEngineRegistry` — one :class:`~app.db.engine.EngineRegistry` per
  shard (primary + optional replica), built lazily from the topology. This is the
  fleet analogue of the single-cluster registry, and the read/write split applies
  *within* each shard for free.
* :class:`EngineBackendFactory` — a :class:`~app.datascale.sharding.proxy.BackendFactory`
  that opens a real :class:`AsyncConnection` on a shard's engine, wrapped so the
  proxy can ``begin``/``commit``/``rollback``/``ping``/``close`` it.
* :class:`SessionShardExecutor` — a :class:`~app.datascale.sharding.executor.ShardExecutor`
  that runs a caller-supplied SQL fragment on each shard's read session and maps
  the rows for the scatter-gather merge.
* :class:`SessionTwoPCParticipant` — a 2PC participant backed by Postgres
  ``PREPARE TRANSACTION`` / ``COMMIT PREPARED`` on one shard.

Everything here is import-safe (no sockets at import/construction); engines and
connections open on first use, matching :mod:`app.db`.
"""

from __future__ import annotations

import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.logging import get_logger
from app.datascale.sharding.executor import Row
from app.datascale.sharding.planner import LogicalQuery, ShardSubquery
from app.datascale.sharding.proxy import BackendConnection
from app.datascale.sharding.topology import Shard, ShardTopology
from app.datascale.sharding.transaction import Vote
from app.db.engine import EngineConfig, EngineRegistry
from app.db.routing import RoutingSessionFactory

logger = get_logger("app.datascale.sharding.backends")

_conn_ids = itertools.count(1)


@dataclass(slots=True)
class ShardEngineRegistry:
    """One :class:`EngineRegistry` per shard, built lazily from the topology.

    Each shard gets its own primary (+ optional replica) engine pair, so the
    existing read/write split works *inside* every shard. Engines are created on
    first access and disposed together. ``pool_size`` / instrumentation are taken
    from a shared :class:`EngineConfig` template (per-shard URL substituted).
    """

    topology: ShardTopology
    instrument: bool = True
    #: Optional knob template; per-shard URLs override the template's ``url``.
    template: EngineConfig | None = None
    _registries: dict[str, EngineRegistry] = field(default_factory=dict)

    def registry_for(self, shard_id: str) -> EngineRegistry:
        """Return (lazily building) the engine registry for ``shard_id``."""
        existing = self._registries.get(shard_id)
        if existing is not None:
            return existing
        shard = self.topology.get(shard_id)
        registry = self._build(shard)
        self._registries[shard_id] = registry
        return registry

    def _build(self, shard: Shard) -> EngineRegistry:
        base = self.template or EngineConfig(url=shard.primary_url)
        primary = base.replace(url=shard.primary_url, application_name=f"shard:{shard.id}")
        replica = (
            base.replace(url=shard.replica_url, application_name=f"shard:{shard.id}:replica")
            if shard.replica_url
            else None
        )
        return EngineRegistry(
            primary_config=primary, replica_config=replica, instrument=self.instrument
        )

    def routing_for(self, shard_id: str) -> RoutingSessionFactory:
        """A read/write-split session factory bound to one shard's engines."""
        return RoutingSessionFactory.from_registry(self.registry_for(shard_id))

    async def dispose(self) -> None:
        """Dispose every built shard registry (call on shutdown)."""
        for shard_id, registry in list(self._registries.items()):
            try:
                await registry.dispose()
            except Exception as exc:  # noqa: BLE001 - shutdown best-effort
                logger.warning("shard.engine.dispose_failed", shard=shard_id, error=str(exc))
        self._registries.clear()


@dataclass(slots=True)
class _EngineBackend:
    """A :class:`BackendConnection` wrapping a real :class:`AsyncConnection`."""

    conn: AsyncConnection
    _id: int = field(default_factory=lambda: next(_conn_ids))
    _txn: Any = field(default=None, init=False)

    @property
    def id(self) -> int:
        return self._id

    async def ping(self) -> bool:
        try:
            await self.conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 - a failed ping ⇒ discard backend
            return False

    async def begin(self) -> None:
        # AsyncConnection auto-begins on first execute; an explicit begin makes
        # the transaction boundary visible for the proxy's transaction pooling.
        if not self.conn.in_transaction():
            self._txn = await self.conn.begin()

    async def commit(self) -> None:
        if self.conn.in_transaction():
            await self.conn.commit()
        self._txn = None

    async def rollback(self) -> None:
        if self.conn.in_transaction():
            await self.conn.rollback()
        self._txn = None

    async def close(self) -> None:
        await self.conn.close()


@dataclass(slots=True)
class EngineBackendFactory:
    """A proxy :class:`BackendFactory` opening connections on one shard's engine.

    Pass ``factory`` (an instance) to a :class:`~app.datascale.sharding.proxy.ConnectionProxy`
    or register one per shard in a :class:`~app.datascale.sharding.proxy.ShardProxyPool`.
    Uses the shard's *primary* engine (the proxy fronts the writer; reads can use
    the per-shard replica via the routing factory directly).
    """

    registry: ShardEngineRegistry
    shard_id: str

    async def __call__(self) -> BackendConnection:
        engine = self.registry.registry_for(self.shard_id).writer()
        conn = await engine.connect()
        return _EngineBackend(conn=conn)


#: A caller-supplied builder that turns a shard subquery + logical query into the
#: per-shard SQL text and bind params for that shard. Kept out of the planner so
#: the sharding layer stays SQL-dialect-agnostic; the table-owner provides it.
SqlFragmentBuilder = Callable[
    [str, ShardSubquery, LogicalQuery], tuple[str, dict[str, Any]]
]


@dataclass(slots=True)
class SessionShardExecutor:
    """A :class:`ShardExecutor` that runs SQL on each shard's read session.

    The ``build_sql`` callback maps the abstract :class:`LogicalQuery` +
    :class:`ShardSubquery` to a concrete SQL string + params for one shard (the
    table owner knows the columns); this executor runs it on that shard's
    read-routed session and returns the rows as dicts for the scatter-gather
    merge. Reads use the per-shard replica when configured (read/write split).
    """

    registry: ShardEngineRegistry
    build_sql: SqlFragmentBuilder

    async def fetch(
        self, shard_id: str, subquery: ShardSubquery, query: LogicalQuery
    ) -> list[Row]:
        sql, params = self.build_sql(shard_id, subquery, query)
        routing = self.registry.routing_for(shard_id)
        async with routing.read() as session:
            result = await session.execute(text(sql), params)
            return [dict(row._mapping) for row in result]  # noqa: SLF001 - row mapping API


@dataclass(slots=True)
class SessionTwoPCParticipant:
    """A 2PC participant backed by Postgres prepared transactions on one shard.

    ``prepare`` runs the work then ``PREPARE TRANSACTION 'gid'`` (durable);
    ``commit`` / ``abort`` run ``COMMIT PREPARED`` / ``ROLLBACK PREPARED``. The
    work itself is a caller-supplied coroutine over the shard connection so this
    participant is reusable for any cross-shard write. The gid is namespaced per
    shard to avoid collisions across the fleet.
    """

    registry: ShardEngineRegistry
    _shard_id: str
    work: Callable[[AsyncConnection], Awaitable[None]]

    @property
    def shard_id(self) -> str:
        return self._shard_id

    def _gid(self, gid: str) -> str:
        # Postgres prepared-transaction identifiers are <= 200 chars; namespace
        # per shard so two shards' GIDs never collide in pg_prepared_xacts.
        return f"{gid}:{self._shard_id}"

    async def prepare(self, gid: str) -> Vote:
        engine = self.registry.registry_for(self._shard_id).writer()
        try:
            async with engine.connect() as conn:
                await conn.begin()
                await self.work(conn)
                await conn.exec_driver_sql(f"PREPARE TRANSACTION '{self._gid(gid)}'")
            return Vote.YES
        except Exception as exc:  # noqa: BLE001 - prepare error ⇒ NO vote
            logger.warning("twopc.session.prepare_failed", shard=self._shard_id, error=str(exc))
            return Vote.NO

    async def commit(self, gid: str) -> None:
        engine = self.registry.registry_for(self._shard_id).writer()
        async with engine.connect() as conn:
            await conn.exec_driver_sql(f"COMMIT PREPARED '{self._gid(gid)}'")

    async def abort(self, gid: str) -> None:
        engine = self.registry.registry_for(self._shard_id).writer()
        async with engine.connect() as conn:
            await conn.exec_driver_sql(f"ROLLBACK PREPARED '{self._gid(gid)}'")


def shard_subquery_sql_default(
    shard_id: str, subquery: ShardSubquery, query: LogicalQuery
) -> tuple[str, dict[str, Any]]:
    """A naive default SQL builder: ``SELECT * FROM <table> [LIMIT n]``.

    Real callers supply their own builder with the right columns/predicates; this
    default is a usable starting point and documents the contract (return a SQL
    string + a bind-params dict). It pushes down the per-shard limit the planner
    computed and applies the order-by the query requested.
    """
    parts = [f"SELECT * FROM {query.table}"]  # noqa: S608 - table is a trusted identifier
    if query.order_by:
        order = ", ".join(f"{s.field} {s.direction.value.upper()}" for s in query.order_by)
        parts.append(f"ORDER BY {order}")
    params: dict[str, Any] = {}
    if subquery.per_shard_limit is not None:
        parts.append("LIMIT :__limit")
        params["__limit"] = subquery.per_shard_limit
    return " ".join(parts), params


__all__ = [
    "EngineBackendFactory",
    "SessionShardExecutor",
    "SessionTwoPCParticipant",
    "ShardEngineRegistry",
    "SqlFragmentBuilder",
    "shard_subquery_sql_default",
]
