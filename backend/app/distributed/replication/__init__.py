"""Multi-region active-active replication — a geo-distributed data layer.

A pure-protocol, dependency-free, exhaustively property-tested implementation of
the building blocks a globally-distributed Kinora needs: hybrid-logical-clock
timestamping, async log shipping + anti-entropy (Merkle) reconciliation,
pluggable conflict resolution (LWW + CRDT registers/sets/counters + app-defined
merge), per-key region affinity + geo-routing, tunable consistency
(ONE/QUORUM/ALL) with bounded staleness, partition detection + healing, and a
deterministic multi-region simulator that *proves* convergence.

This package is distinct from :mod:`app.memory.crdt` (the canon entity-versioning
CRDT): that is per-entity within one store; this is a cluster-wide replication
protocol across regions. See ``DESIGN.md`` for the architecture.

The public surface is re-exported here for ergonomic imports; submodules carry
the full docstrings. Importing this package pulls in only pure stdlib code.
"""

from __future__ import annotations

from app.distributed.replication.antientropy import (
    MerkleDigest,
    Reconciler,
    SyncRequest,
    SyncResponse,
    merkle_digest,
    plan_delta_sync,
    plan_merkle_repair,
)
from app.distributed.replication.clock import (
    DEFAULT_MAX_SKEW_MS,
    HybridLogicalClock,
    HybridTimestamp,
    ManualClock,
    NodeId,
    PhysicalClock,
)
from app.distributed.replication.conflict import (
    ConflictResolver,
    CustomResolver,
    Dot,
    GCounterResolver,
    GCounterValue,
    LWWRegisterValue,
    LWWResolver,
    MVRegisterResolver,
    MVRegisterValue,
    NonConvergentMergeError,
    ORSetResolver,
    ORSetValue,
    PNCounterResolver,
    PNCounterValue,
    ResolverRegistry,
    Stamped,
)
from app.distributed.replication.consistency import (
    ConsistencyLevel,
    ReadCoordinator,
    ReadResult,
    ReplicaAnswer,
    StalenessPolicy,
    WriteAck,
    WriteCoordinator,
    WriteOutcome,
    quorum_overlaps,
)
from app.distributed.replication.failure import (
    FailureDetector,
    PartitionEvent,
    PartitionEventKind,
    PartitionMonitor,
    PeerState,
    PhiDetector,
)
from app.distributed.replication.gossip import GossipEngine, TickReport
from app.distributed.replication.log import (
    OpKind,
    ReplicationLog,
    ReplicationRecord,
    WriteOp,
)
from app.distributed.replication.merkle import (
    MerkleTree,
    build_merkle,
    diff_buckets,
)
from app.distributed.replication.node import IngestResult, ReplicaNode, WriteReceipt
from app.distributed.replication.routing import (
    GeoRouter,
    NoRouteError,
    PlacementPolicy,
    RegionTopology,
    RouteDecision,
)
from app.distributed.replication.simulator import (
    ConvergenceReport,
    MultiRegionSimulator,
    Scenario,
    assert_converged,
)
from app.distributed.replication.store import Cell, KeyAffinity, ReplicaStore
from app.distributed.replication.transport import (
    DirectTransport,
    FabricConfig,
    InMemoryFabric,
    Message,
    Partition,
    Transport,
)
from app.distributed.replication.version import VersionVector, join_all

__all__ = [
    # clock
    "NodeId",
    "HybridTimestamp",
    "HybridLogicalClock",
    "ManualClock",
    "PhysicalClock",
    "DEFAULT_MAX_SKEW_MS",
    # version
    "VersionVector",
    "join_all",
    # conflict
    "ConflictResolver",
    "Stamped",
    "Dot",
    "LWWResolver",
    "LWWRegisterValue",
    "GCounterValue",
    "GCounterResolver",
    "PNCounterValue",
    "PNCounterResolver",
    "ORSetValue",
    "ORSetResolver",
    "MVRegisterValue",
    "MVRegisterResolver",
    "CustomResolver",
    "NonConvergentMergeError",
    "ResolverRegistry",
    # log / store
    "OpKind",
    "WriteOp",
    "ReplicationRecord",
    "ReplicationLog",
    "Cell",
    "KeyAffinity",
    "ReplicaStore",
    # node
    "ReplicaNode",
    "WriteReceipt",
    "IngestResult",
    # merkle / anti-entropy
    "MerkleTree",
    "build_merkle",
    "diff_buckets",
    "Reconciler",
    "SyncRequest",
    "SyncResponse",
    "MerkleDigest",
    "merkle_digest",
    "plan_delta_sync",
    "plan_merkle_repair",
    # consistency
    "ConsistencyLevel",
    "WriteCoordinator",
    "WriteOutcome",
    "WriteAck",
    "ReadCoordinator",
    "ReadResult",
    "ReplicaAnswer",
    "StalenessPolicy",
    "quorum_overlaps",
    # routing
    "RegionTopology",
    "PlacementPolicy",
    "GeoRouter",
    "RouteDecision",
    "NoRouteError",
    # failure / partition
    "FailureDetector",
    "PhiDetector",
    "PartitionMonitor",
    "PartitionEvent",
    "PartitionEventKind",
    "PeerState",
    # transport
    "Transport",
    "InMemoryFabric",
    "DirectTransport",
    "FabricConfig",
    "Partition",
    "Message",
    # gossip
    "GossipEngine",
    "TickReport",
    # simulator
    "MultiRegionSimulator",
    "Scenario",
    "ConvergenceReport",
    "assert_converged",
]
