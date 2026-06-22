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
"""

from __future__ import annotations

from app.memory.budget_service import (
    BudgetExceeded,
    BudgetLimits,
    BudgetService,
    Reservation,
)
from app.memory.cache_service import CacheLookup, CacheService
from app.memory.canon_service import CanonService, UnknownBeatError
from app.memory.canon_vault import CanonVault, VaultExport
from app.memory.episodic_service import EpisodicService
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

__all__ = [
    "BlobStore",
    "BudgetExceeded",
    "BudgetLimits",
    "BudgetService",
    "CacheLookup",
    "CacheService",
    "CanonEntitySlice",
    "CanonService",
    "CanonSlice",
    "CanonVault",
    "Embedder",
    "EndpointFrame",
    "EpisodicService",
    "EpisodicShotRef",
    "NotWired",
    "NotWiredRenderEnqueuer",
    "NotWiredShotPlanner",
    "PreferencePrior",
    "PreferencePriors",
    "PrefsService",
    "RefImage",
    "RenderEnqueuer",
    "Reservation",
    "ShotPlanner",
    "ShotSpec",
    "StateSlice",
    "UnknownBeatError",
    "VaultExport",
]
