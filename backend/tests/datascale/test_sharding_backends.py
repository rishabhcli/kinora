"""Unit tests for the production backend adapters that need no infra.

The engine/session/2PC paths require a live Postgres and are covered by
integration tests (skipped when no test DB). What we can prove deterministically
here: import-safety (no sockets at construction), the default SQL fragment
builder, per-shard engine registry wiring, and the 2PC gid namespacing.
"""

from __future__ import annotations

import pytest

from app.datascale.sharding.backends import (
    EngineBackendFactory,
    SessionTwoPCParticipant,
    ShardEngineRegistry,
    shard_subquery_sql_default,
)
from app.datascale.sharding.planner import LogicalQuery, ShardSubquery, SortDir, SortKey
from app.datascale.sharding.topology import Shard, ShardTopology


def _topo() -> ShardTopology:
    return ShardTopology.of(
        Shard(id="s1", primary_url="postgresql+asyncpg://h1/db", replica_url="postgresql+asyncpg://r1/db"),
        Shard(id="s2", primary_url="postgresql+asyncpg://h2/db"),
    )


def test_constructing_registry_opens_no_socket() -> None:
    # No engine is built until first access — pure construction is safe.
    reg = ShardEngineRegistry(topology=_topo())
    assert reg._registries == {}  # nothing built yet


def test_registry_builds_per_shard_with_replica() -> None:
    reg = ShardEngineRegistry(topology=_topo())
    r1 = reg.registry_for("s1")
    assert r1.has_replica  # s1 has a replica URL
    r2 = reg.registry_for("s2")
    assert not r2.has_replica  # s2 has none
    # Building is idempotent / cached.
    assert reg.registry_for("s1") is r1


def test_registry_unknown_shard_raises() -> None:
    reg = ShardEngineRegistry(topology=_topo())
    with pytest.raises(KeyError):
        reg.registry_for("nope")


def test_engine_backend_factory_is_constructible() -> None:
    reg = ShardEngineRegistry(topology=_topo())
    factory = EngineBackendFactory(registry=reg, shard_id="s1")
    # Construction does not open a connection (callable, not yet called).
    assert factory.shard_id == "s1"


def test_default_sql_builder_pushes_limit_and_order() -> None:
    query = LogicalQuery(
        table="books",
        order_by=(SortKey("created_at", SortDir.DESC),),
        limit=10,
    )
    sq = ShardSubquery(shard_id="s1", per_shard_limit=10)
    sql, params = shard_subquery_sql_default("s1", sq, query)
    assert "SELECT * FROM books" in sql
    assert "ORDER BY created_at DESC" in sql
    assert "LIMIT :__limit" in sql
    assert params["__limit"] == 10


def test_default_sql_builder_no_limit_when_unbounded() -> None:
    query = LogicalQuery(table="books")
    sq = ShardSubquery(shard_id="s1", per_shard_limit=None)
    sql, params = shard_subquery_sql_default("s1", sq, query)
    assert "LIMIT" not in sql
    assert params == {}


async def test_twopc_participant_namespaces_gid_per_shard() -> None:
    reg = ShardEngineRegistry(topology=_topo())

    async def work(_conn: object) -> None:  # pragma: no cover - not invoked
        return None

    p = SessionTwoPCParticipant(registry=reg, _shard_id="s1", work=work)  # type: ignore[arg-type]
    assert p.shard_id == "s1"
    assert p._gid("txn-42") == "txn-42:s1"
