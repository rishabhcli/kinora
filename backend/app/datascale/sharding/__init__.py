"""Horizontal sharding + connection proxy (built on top of ``app.db``).

This package is the horizontal-scale data plane: a fleet of Postgres clusters
("shards"), a strategy framework that maps a key to its shard, a router and a
cross-shard query planner/executor, online resharding, and a pgbouncer-shaped
connection proxy. It composes the single-cluster primitives in :mod:`app.db`
(engine registry, read/write split, pool health) rather than replacing them.

Module map (all under ``app.datascale.sharding``):

* :mod:`~app.datascale.sharding.keys` — :class:`ShardKey` value object: a stable,
  deterministic byte encoding of the column(s) routing is based on.
* :mod:`~app.datascale.sharding.topology` — :class:`Shard` / :class:`ShardTopology`:
  the immutable description of the shard fleet + per-shard lifecycle state.
* :mod:`~app.datascale.sharding.strategy` — the placement policies:
  :class:`ModuloHashStrategy`, :class:`RangeStrategy`, :class:`DirectoryStrategy`,
  :class:`ConsistentHashStrategy` (virtual nodes).
* :mod:`~app.datascale.sharding.router` — :class:`ShardRouter`: resolves a query
  to its target shard(s), applying state-aware write/read safety.
* :mod:`~app.datascale.sharding.planner` — :class:`QueryPlanner`: turns a logical
  cross-shard query into a fan-out :class:`ScatterPlan` (single-shard vs
  scatter-gather, with aggregation/sort/limit push-down rules).
* :mod:`~app.datascale.sharding.executor` — :class:`ScatterGatherExecutor`:
  runs a plan across shards, merges/aggregates/sorts/limits the partials.
* :mod:`~app.datascale.sharding.transaction` — the distributed-transaction seam:
  a two-phase-commit coordinator and a saga runner with compensations.
* :mod:`~app.datascale.sharding.resharding` — online split/move: dual-write,
  backfill, verify, cutover, with a rollback-safe state machine.
* :mod:`~app.datascale.sharding.proxy` — the connection proxy / pooler:
  transaction pooling, multiplexing, a bounded wait queue, and health checks.
* :mod:`~app.datascale.sharding.backends` — production adapters binding the
  abstract seams to real per-shard engines / sessions / prepared transactions.
* :mod:`~app.datascale.sharding.cluster` — :class:`ShardCluster`, the one facade
  composing router + planner + executor + resharding.
* :mod:`~app.datascale.sharding.slots` — fixed hash-slot sharding (Redis-Cluster
  shaped): a :class:`SlotMap` + :class:`SlotStrategy` so resharding is slot
  reassignment, not a per-key move.
* :mod:`~app.datascale.sharding.rebalance` — the rebalance planner: turn an
  add/remove-shard topology change into an ordered, estimated set of moves.
"""

from __future__ import annotations

from app.datascale.sharding.cluster import ShardCluster
from app.datascale.sharding.executor import (
    FailureMode,
    GatherResult,
    ScatterGatherExecutor,
    ShardExecutor,
)
from app.datascale.sharding.keys import ShardKey, ShardKeyValue, coerce_key
from app.datascale.sharding.planner import (
    Aggregate,
    AggregateOp,
    LogicalQuery,
    QueryPlanner,
    ScatterPlan,
    SortDir,
    SortKey,
)
from app.datascale.sharding.proxy import (
    ConnectionProxy,
    PoolError,
    ProxyConfig,
    ShardProxyPool,
)
from app.datascale.sharding.rebalance import (
    RebalancePlan,
    plan_ring_rebalance,
    plan_slot_rebalance,
)
from app.datascale.sharding.resharding import (
    ReshardingJob,
    ReshardPlan,
    ReshardState,
)
from app.datascale.sharding.router import Access, MigrationOverlay, Resolution, ShardRouter
from app.datascale.sharding.slots import SlotMap, SlotStrategy, migration_set
from app.datascale.sharding.strategy import (
    ConsistentHashStrategy,
    DirectoryStrategy,
    ModuloHashStrategy,
    RangeBound,
    RangeStrategy,
    RoutingError,
    ShardStrategy,
)
from app.datascale.sharding.topology import Shard, ShardState, ShardTopology
from app.datascale.sharding.transaction import (
    SagaCoordinator,
    SagaStep,
    TwoPhaseCommitCoordinator,
)

__all__ = [
    "Access",
    "Aggregate",
    "AggregateOp",
    "ConnectionProxy",
    "ConsistentHashStrategy",
    "DirectoryStrategy",
    "FailureMode",
    "GatherResult",
    "LogicalQuery",
    "MigrationOverlay",
    "ModuloHashStrategy",
    "PoolError",
    "ProxyConfig",
    "QueryPlanner",
    "RangeBound",
    "RangeStrategy",
    "RebalancePlan",
    "ReshardPlan",
    "ReshardState",
    "ReshardingJob",
    "Resolution",
    "RoutingError",
    "SagaCoordinator",
    "SagaStep",
    "ScatterGatherExecutor",
    "ScatterPlan",
    "Shard",
    "ShardCluster",
    "ShardExecutor",
    "ShardKey",
    "ShardKeyValue",
    "ShardProxyPool",
    "ShardRouter",
    "ShardState",
    "ShardStrategy",
    "ShardTopology",
    "SlotMap",
    "SlotStrategy",
    "SortDir",
    "SortKey",
    "TwoPhaseCommitCoordinator",
    "coerce_key",
    "migration_set",
    "plan_ring_rebalance",
    "plan_slot_rebalance",
]
