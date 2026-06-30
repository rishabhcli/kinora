"""End-to-end render reliability coordinator (additive; FINAL round).

*Render this shot reliably, across any available providers, honoring budget + SLA
+ quality.* :class:`~app.video.reliability.coordinator.ReliableRenderCoordinator`
is the single high-level entry point that ties the round-1/round-2 primitives
together — the provider router (failover/hedge), the capacity/SLA governor, the
cost budget, the quality gate, the quality-reputation signal, and the async job
sink — behind minimal *local* Protocols so it can be built and tested before any
of those rounds is merged. The orchestrator binds the real implementations later.

What it guarantees, per shot:

* a **ranked** provider candidate list (governor admission + budget pre-flight +
  reputation / load-headroom / cost weighting), see :mod:`.candidates`;
* attempts in rank order with bounded coordinator-level retries on top of the
  router's own failover/hedge;
* a **cost reservation per attempt**, released on failure/rejection, settled only
  on a passing result — and a clean **budget-abort** when a reservation is denied;
* a **quality gate** that *escalates* a poor clip to the next-best provider rather
  than shipping garbage (§10);
* an overall per-shot **deadline** that returns the best-so-far before the reader
  arrives;
* a structured :class:`~app.video.reliability.models.RenderAttemptLog` recording
  every provider tried and why each failed / was rejected / won;
* a final **graceful fallback** (best-so-far, else a degraded-but-real narrated
  text card) — it **never** silently returns nothing.

Pure given its collaborators: injectable clock + sleep, no infra, no network, and
never touches the ``KINORA_LIVE_VIDEO`` spend gate.
"""

from __future__ import annotations

from app.video.reliability.candidates import (
    Candidate,
    CandidatePlan,
    PrunedCandidate,
    build_candidates,
)
from app.video.reliability.clock import Clock, ManualClock, Sleep, make_manual_sleep
from app.video.reliability.config import ReliabilityConfig
from app.video.reliability.coordinator import ReliableRenderCoordinator
from app.video.reliability.models import (
    AttemptRecord,
    AttemptStatus,
    FallbackReason,
    RenderAttemptLog,
    RenderOutcome,
    RenderResult,
    RenderTier,
    ShotSpec,
)
from app.video.reliability.protocols import (
    BudgetReservation,
    CostBudgetProtocol,
    GovernorProtocol,
    JobSinkProtocol,
    QualityGateProtocol,
    QualityReputationProtocol,
    RouterProtocol,
)

__all__ = [
    "AttemptRecord",
    "AttemptStatus",
    "BudgetReservation",
    "Candidate",
    "CandidatePlan",
    "Clock",
    "CostBudgetProtocol",
    "FallbackReason",
    "GovernorProtocol",
    "JobSinkProtocol",
    "ManualClock",
    "PrunedCandidate",
    "QualityGateProtocol",
    "QualityReputationProtocol",
    "ReliabilityConfig",
    "ReliableRenderCoordinator",
    "RenderAttemptLog",
    "RenderOutcome",
    "RenderResult",
    "RenderTier",
    "RouterProtocol",
    "ShotSpec",
    "Sleep",
    "build_candidates",
    "make_manual_sleep",
]
