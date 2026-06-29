"""ShardCluster: the one ergonomic facade that composes the whole sharding stack.

Everything else in this package is a focused, independently-testable piece. A
caller that just wants "sharded data access" should not have to wire seven of
them together by hand. :class:`ShardCluster` is that wiring: given a topology and
a strategy it exposes

* :meth:`router` — the state-aware :class:`~app.datascale.sharding.router.ShardRouter`.
* :meth:`planner` — the :class:`~app.datascale.sharding.planner.QueryPlanner`.
* :meth:`run_query` — plan + scatter-gather a :class:`LogicalQuery` end-to-end
  through a supplied :class:`ShardExecutor` (production: the session executor).
* :meth:`begin_reshard` — start an online resharding job over a mover, returning
  the :class:`ReshardingJob` whose overlay is auto-published back into the live
  router (so reads/writes follow the migration with no caller changes).
* :meth:`proxy_pool` / engine registry — the connection-proxy + per-shard
  engines for the executor and 2PC participants to use.

The facade keeps a single mutable :class:`~app.datascale.sharding.router.MigrationOverlay`
so a reshard in flight is reflected everywhere the cluster routes. It is the
object a table-family owner would hold (one cluster for book-keyed tables,
another for user-keyed) — additive and opt-in; nothing constructs it unless a
deployment turns sharding on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.datascale.sharding.executor import (
    FailureMode,
    GatherResult,
    ScatterGatherExecutor,
    ShardExecutor,
)
from app.datascale.sharding.planner import LogicalQuery, QueryPlanner, ScatterPlan
from app.datascale.sharding.proxy import ProxyConfig, ShardProxyPool
from app.datascale.sharding.resharding import (
    OverlayPublisher,
    ReshardDataMover,
    ReshardingJob,
    ReshardPlan,
)
from app.datascale.sharding.router import MigrationOverlay, ShardRouter
from app.datascale.sharding.strategy import ShardStrategy
from app.datascale.sharding.topology import ShardTopology

logger = get_logger("app.datascale.sharding.cluster")


@dataclass(slots=True)
class ShardCluster:
    """Composes router + planner + executor + resharding over one strategy.

    Construct with :meth:`build` (the common path) or directly. The cluster owns
    the live :class:`MigrationOverlay`; when a reshard publishes a new overlay the
    cluster swaps it in atomically so :meth:`router` always reflects the current
    migration state.
    """

    strategy: ShardStrategy
    topology: ShardTopology
    _overlay: MigrationOverlay = field(default_factory=MigrationOverlay)

    @classmethod
    def build(cls, strategy: ShardStrategy, topology: ShardTopology) -> ShardCluster:
        """Build a cluster over a strategy + topology (empty migration overlay)."""
        return cls(strategy=strategy, topology=topology)

    # -- routing / planning -------------------------------------------------- #

    def router(self) -> ShardRouter:
        """The state-aware router reflecting the current migration overlay."""
        return ShardRouter(strategy=self.strategy, topology=self.topology, overlay=self._overlay)

    def planner(self) -> QueryPlanner:
        """A query planner over the current router."""
        return QueryPlanner(self.router())

    def plan(self, query: LogicalQuery) -> ScatterPlan:
        """Plan a cross-shard query (pure)."""
        return self.planner().plan(query)

    async def run_query(
        self,
        query: LogicalQuery,
        executor: ShardExecutor,
        *,
        failure_mode: FailureMode = FailureMode.FAIL_FAST,
    ) -> GatherResult:
        """Plan ``query`` then scatter-gather it through ``executor``, end-to-end."""
        plan = self.plan(query)
        return await ScatterGatherExecutor(executor, failure_mode).execute(plan)

    # -- migration overlay --------------------------------------------------- #

    @property
    def overlay(self) -> MigrationOverlay:
        """The live migration overlay (drives dual-write / cutover routing)."""
        return self._overlay

    def _publisher(self) -> OverlayPublisher:
        """A publisher the resharding job calls to swap in each new overlay."""

        async def publish(overlay: MigrationOverlay) -> None:
            self._overlay = overlay
            logger.info("cluster.overlay_updated", moving=len(overlay.moves))

        return publish

    def begin_reshard(self, plan: ReshardPlan, mover: ReshardDataMover) -> ReshardingJob:
        """Create a resharding job whose overlay auto-publishes into this cluster.

        The returned job is *not* started; drive it with ``await job.run()`` (full
        protocol) or phase-by-phase. As it transitions, the cluster's router
        starts dual-writing then cuts over to the target with no caller change.
        """
        return ReshardingJob(plan=plan, mover=mover, publish=self._publisher())

    # -- connection plane ---------------------------------------------------- #

    def proxy_pool(
        self,
        factories: dict[str, object],
        *,
        config: ProxyConfig | None = None,
    ) -> ShardProxyPool:
        """A connection-proxy pool over per-shard backend factories.

        ``factories`` maps each shard id to a
        :class:`~app.datascale.sharding.proxy.BackendFactory` (production:
        :class:`~app.datascale.sharding.backends.EngineBackendFactory`).
        """
        return ShardProxyPool(factories=factories, config=config or ProxyConfig())  # type: ignore[arg-type]


__all__ = ["ShardCluster"]
