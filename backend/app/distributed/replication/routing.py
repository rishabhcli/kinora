"""Geo-routing: per-key region affinity, replica placement, and request routing.

In active-active *any* region can serve *any* key, but where you route a request
matters for latency and for honouring a key's home region. This module is the
pure decision logic:

* :class:`RegionTopology` — the static map of regions, their nodes, and the
  pairwise latency matrix. The source of truth for "who is near whom".
* :class:`PlacementPolicy` — given a key and its
  :class:`~app.distributed.replication.store.KeyAffinity`, which regions hold a
  replica. Full replication by default; affinity narrows it.
* :class:`GeoRouter` — picks the node to coordinate a read or write for a key
  from a *client region*: prefer the key's home region, else the lowest-latency
  replica reachable from the client, with deterministic tiebreaks. Honours a
  liveness predicate so a partitioned/dead region is skipped.

Pure: latency is data, liveness is an injected predicate, no clock or network.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from app.distributed.replication.clock import NodeId
from app.distributed.replication.store import KeyAffinity

#: A predicate answering "is this node currently reachable/healthy?".
LivenessFn = Callable[[NodeId], bool]


@dataclass(frozen=True, slots=True)
class RegionTopology:
    """Regions, the nodes in each, and the pairwise inter-region latency matrix.

    ``latency_ms[(a, b)]`` is the one-way latency from region ``a`` to region
    ``b``; a missing pair falls back to :attr:`default_latency_ms`. Same-region
    latency is ``0`` unless overridden.
    """

    nodes_by_region: Mapping[str, frozenset[NodeId]]
    latency_ms: Mapping[tuple[str, str], int] = field(default_factory=dict)
    default_latency_ms: int = 100

    @property
    def regions(self) -> frozenset[str]:
        return frozenset(self.nodes_by_region)

    def nodes(self, region: str) -> frozenset[NodeId]:
        return self.nodes_by_region.get(region, frozenset())

    def all_nodes(self) -> frozenset[NodeId]:
        out: set[NodeId] = set()
        for group in self.nodes_by_region.values():
            out |= group
        return frozenset(out)

    def latency(self, src_region: str, dst_region: str) -> int:
        if src_region == dst_region:
            return self.latency_ms.get((src_region, dst_region), 0)
        return self.latency_ms.get((src_region, dst_region), self.default_latency_ms)

    @classmethod
    def from_nodes(
        cls,
        nodes: Iterable[NodeId],
        latency_ms: Mapping[tuple[str, str], int] | None = None,
        default_latency_ms: int = 100,
    ) -> RegionTopology:
        """Build a topology by grouping ``nodes`` by their region."""
        grouped: dict[str, set[NodeId]] = {}
        for node in nodes:
            grouped.setdefault(node.region, set()).add(node)
        return cls(
            {r: frozenset(ns) for r, ns in grouped.items()},
            latency_ms or {},
            default_latency_ms,
        )


class PlacementPolicy:
    """Decides which regions replicate a key, given its affinity (or none)."""

    def __init__(self, topology: RegionTopology) -> None:
        self._topology = topology

    def replica_regions(self, affinity: KeyAffinity | None) -> frozenset[str]:
        """The regions that hold a replica of a key with ``affinity``.

        No affinity -> full active-active (every region). Affinity with an
        explicit ``replicas`` set -> exactly those (plus the home region, which
        is always a replica). Affinity with empty ``replicas`` -> all regions.
        """
        if affinity is None:
            return self._topology.regions
        if not affinity.replicas:
            return self._topology.regions
        return frozenset(affinity.replicas) | {affinity.home_region}

    def replica_nodes(self, affinity: KeyAffinity | None) -> frozenset[NodeId]:
        regions = self.replica_regions(affinity)
        out: set[NodeId] = set()
        for region in regions:
            out |= self._topology.nodes(region)
        return frozenset(out)


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """The chosen coordinator node and why."""

    node: NodeId
    reason: str
    latency_ms: int


class NoRouteError(RuntimeError):
    """Raised when no live replica can serve a key from the client region."""


class GeoRouter:
    """Routes reads/writes to a coordinator node for a key from a client region."""

    def __init__(
        self,
        topology: RegionTopology,
        placement: PlacementPolicy | None = None,
        liveness: LivenessFn | None = None,
    ) -> None:
        self._topology = topology
        self._placement = placement or PlacementPolicy(topology)
        self._liveness = liveness or (lambda _node: True)

    def _live_nodes(self, region: str) -> list[NodeId]:
        return sorted(n for n in self._topology.nodes(region) if self._liveness(n))

    def route(
        self,
        key: str,
        client_region: str,
        affinity: KeyAffinity | None = None,
    ) -> RouteDecision:
        """Pick the coordinator for ``key`` requested from ``client_region``.

        Preference order, each restricted to *live* replica nodes of the key:

        1. A node in the client's own region (zero cross-region latency).
        2. A node in the key's home region (affinity-honouring).
        3. The lowest-latency replica region reachable from the client.

        Ties break on :class:`NodeId` order for determinism. Raises
        :class:`NoRouteError` if no live replica exists.
        """
        replica_regions = self._placement.replica_regions(affinity)

        # 1. local region, if it replicates the key and has a live node.
        if client_region in replica_regions:
            local = self._live_nodes(client_region)
            if local:
                return RouteDecision(local[0], "local-region", 0)

        # 2. home region (affinity), if live.
        if affinity is not None and affinity.home_region in replica_regions:
            home = self._live_nodes(affinity.home_region)
            if home:
                lat = self._topology.latency(client_region, affinity.home_region)
                return RouteDecision(home[0], "home-region", lat)

        # 3. nearest live replica region.
        candidates: list[tuple[int, NodeId]] = []
        for region in replica_regions:
            lat = self._topology.latency(client_region, region)
            for node in self._live_nodes(region):
                candidates.append((lat, node))
        if not candidates:
            raise NoRouteError(f"no live replica for key {key!r} from {client_region}")
        candidates.sort(key=lambda c: (c[0], c[1]))
        lat, node = candidates[0]
        return RouteDecision(node, "nearest-replica", lat)

    def replica_set(self, affinity: KeyAffinity | None = None) -> frozenset[NodeId]:
        """The full set of replica nodes for a key (for consistency coordination)."""
        return self._placement.replica_nodes(affinity)

    def live_replica_set(self, affinity: KeyAffinity | None = None) -> frozenset[NodeId]:
        """Replica nodes that are currently live (the reachable quorum candidates)."""
        return frozenset(n for n in self.replica_set(affinity) if self._liveness(n))
