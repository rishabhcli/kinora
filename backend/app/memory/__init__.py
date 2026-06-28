"""The MCP canon-memory service business layer (kinora.md §8) — Track 1.

Clean services over the existing repositories + providers:

* :class:`~app.memory.canon_service.CanonService` — the ``canon.query`` retrieval
  policy (§8.4), time-travel reads, versioned writes, and forgetting (§8.5);
* :class:`~app.memory.episodic_service.EpisodicService` — the "what worked
  before" vector store (§8.2);
* :class:`~app.memory.cache_service.CacheService` — the content-hash shot cache
  (§8.7);
* :class:`~app.memory.budget_service.BudgetService` — the hard video-seconds cap
  (§11.1);
* :class:`~app.memory.prefs_service.PrefsService` — cross-session preference
  learning (§8.6);
* :class:`~app.memory.canon_vault.CanonVault` — the inspectable markdown vault
  (§8.1).

The :mod:`app.memory.interfaces` module holds the JSON-serializable contracts
(``CanonSlice``, ``ShotSpec``) and the DI seams owned by later phases
(``RenderEnqueuer``, ``ShotPlanner``).

**Bitemporal knowledge-graph engine (§8).** Layered alongside the above (parallel,
opt-in, additive): :class:`~app.memory.temporal_state_service.TemporalStateService`
(VALID-time AND TRANSACTION-time facts; time-travel ``as_of`` reads),
:mod:`~app.memory.crdt` (conflict-free concurrent writes),
:class:`~app.memory.branch_service.BranchService` (canon FORK / DIFF / MERGE),
:class:`~app.memory.audit_log.AuditLog` (append-only hash-chained provenance),
:mod:`~app.memory.graph_reasoning` + :mod:`~app.memory.retrieval` +
:class:`~app.memory.canon_reasoner.CanonReasoner` (graph reasoning + scalable retrieval),
:class:`~app.memory.compaction.TemporalCompactor` (bounded tx-history), and
:class:`~app.memory.bitemporal_vault.BitemporalVault` (the inspectable read contract).
"""

from __future__ import annotations

from app.memory.audit_log import AuditLog
from app.memory.bitemporal import (
    Allen,
    BeatInterval,
    BitemporalCoord,
    TxInterval,
    utcnow,
)
from app.memory.bitemporal_vault import BitemporalVault, BitemporalVaultDoc
from app.memory.branch_service import BranchError, BranchService
from app.memory.budget_service import (
    BudgetExceeded,
    BudgetLimits,
    BudgetService,
    Reservation,
)
from app.memory.cache_service import CacheLookup, CacheService
from app.memory.canon_reasoner import CanonReasoner
from app.memory.canon_service import CanonService, UnknownBeatError
from app.memory.canon_vault import CanonVault, VaultExport
from app.memory.compaction import CompactionPlan, CompactionResult, TemporalCompactor
from app.memory.contracts import (
    AuditChain,
    AuditEntry,
    BitemporalFact,
    BranchDiff,
    BranchInfo,
    CanonReadView,
    FactChange,
    FactHistory,
    MergeConflict,
    MergeResult,
)
from app.memory.crdt import (
    HLC,
    GCounter,
    HLCClock,
    LWWRegister,
    ORSet,
    Stamp,
    VersionVector,
)
from app.memory.episodic_service import EpisodicService
from app.memory.graph_reasoning import (
    CanonGraph,
    Contradiction,
    Edge,
    find_contradictions,
)
from app.memory.interfaces import (
    BlobStore,
    CanonEntitySlice,
    CanonSlice,
    Embedder,
    EndpointFrame,
    EpisodicShotRef,
    NotWired,
    NotWiredRenderEnqueuer,
    NotWiredShotPlanner,
    RefImage,
    RenderEnqueuer,
    ShotPlanner,
    ShotSpec,
    StateSlice,
)
from app.memory.prefs_service import PreferencePrior, PreferencePriors, PrefsService
from app.memory.temporal_state_service import (
    FactNotFoundError,
    TemporalStateService,
)

__all__ = [
    "HLC",
    "Allen",
    "AuditChain",
    "AuditEntry",
    "AuditLog",
    "BeatInterval",
    "BitemporalCoord",
    "BitemporalFact",
    "BitemporalVault",
    "BitemporalVaultDoc",
    "BlobStore",
    "BranchDiff",
    "BranchError",
    "BranchInfo",
    "BranchService",
    "BudgetExceeded",
    "BudgetLimits",
    "BudgetService",
    "CacheLookup",
    "CacheService",
    "CanonEntitySlice",
    "CanonGraph",
    "CanonReadView",
    "CanonReasoner",
    "CanonService",
    "CanonSlice",
    "CanonVault",
    "CompactionPlan",
    "CompactionResult",
    "Contradiction",
    "Edge",
    "Embedder",
    "EndpointFrame",
    "EpisodicService",
    "EpisodicShotRef",
    "FactChange",
    "FactHistory",
    "FactNotFoundError",
    "GCounter",
    "HLCClock",
    "LWWRegister",
    "MergeConflict",
    "MergeResult",
    "NotWired",
    "NotWiredRenderEnqueuer",
    "NotWiredShotPlanner",
    "ORSet",
    "PreferencePrior",
    "PreferencePriors",
    "PrefsService",
    "RefImage",
    "RenderEnqueuer",
    "Reservation",
    "ShotPlanner",
    "ShotSpec",
    "Stamp",
    "StateSlice",
    "TemporalCompactor",
    "TemporalStateService",
    "TxInterval",
    "UnknownBeatError",
    "VaultExport",
    "VersionVector",
    "find_contradictions",
    "utcnow",
]
