"""Inference request router — facet A of the inference gateway.

A high-throughput LLM-inference scheduling brain (see ``DESIGN.md``):

* **continuous / in-flight batching** with token-budget bin-packing
  (:mod:`~app.inference.router.binpack`);
* **priority + weighted-fair-share** scheduling across tenants/agents
  (:mod:`~app.inference.router.fairshare`);
* **admission control + backpressure + queue-time SLAs**
  (:mod:`~app.inference.router.admission`);
* **KV-cache-affinity routing** — route same-prefix requests to the same worker
  (:mod:`~app.inference.router.affinity`);
* **request coalescing** — identical in-flight requests pay once
  (:mod:`~app.inference.router.coalescing`);
* a clean **``InferenceBackend`` protocol** + cross-facet seams
  (:mod:`~app.inference.router.protocols`);
* a **deterministic simulator** validating fairness + throughput
  (:mod:`~app.inference.router.simulator`).

Cites kinora.md §11 (model stack / budget) and §12 (queue, concurrency,
backpressure, caching, observability). Every model call is behind an injected
:class:`InferenceBackend`; the bundled defaults are deterministic fakes — zero
live calls.
"""

from __future__ import annotations

from app.inference.router.admission import (
    AdmissionConfig,
    AdmissionController,
    AdmissionDecision,
    LoadSnapshot,
    QueueTimeSLA,
    RejectReason,
)
from app.inference.router.affinity import (
    AffinityConfig,
    AffinityRouter,
    ResidencyOracle,
    WorkerResolver,
    WorkerScore,
)
from app.inference.router.backends import ChatProviderBackend, EchoBackend
from app.inference.router.binpack import (
    BatchBudget,
    PackResult,
    TokenBinPacker,
    total_tokens,
)
from app.inference.router.cancellation import (
    CancellationRegistry,
    CancellationToken,
    CancelledError,
)
from app.inference.router.coalescing import CoalesceOutcome, CoalescingTable
from app.inference.router.dispatcher import MultiModelRouter
from app.inference.router.errors import (
    AdmissionRejected,
    BackendError,
    NoEligibleWorker,
    QueueTimeSLAExpired,
    RouterConfigError,
    RouterError,
)
from app.inference.router.factory import build_multi_model_router, build_router
from app.inference.router.fairshare import FairShareConfig, FairShareScheduler
from app.inference.router.metrics import P2Quantile, RouterStats
from app.inference.router.planner import BatchPlan, BatchPlanner, PlannerConfig
from app.inference.router.protocols import (
    Clock,
    InferenceBackend,
    InferenceResult,
    PrefixCacheOracle,
    WorkerPoolController,
    WorkerView,
)
from app.inference.router.request import (
    TERMINAL_STATES,
    InferenceRequest,
    RequestPriority,
    RequestState,
    prefix_key_for,
)
from app.inference.router.router import InferenceRouter, RouterConfig, TransitionHook
from app.inference.router.simulator import (
    RouterSimulator,
    ScenarioConfig,
    SimBackend,
    SimReport,
    TenantSpec,
    VirtualClock,
)
from app.inference.router.worker import Worker, WorkerConfig, WorkerPool

__all__ = [
    "TERMINAL_STATES",
    "AdmissionConfig",
    "AdmissionController",
    "AdmissionDecision",
    "AdmissionRejected",
    "AffinityConfig",
    "AffinityRouter",
    "BackendError",
    "BatchBudget",
    "BatchPlan",
    "BatchPlanner",
    "CancellationRegistry",
    "CancellationToken",
    "CancelledError",
    "ChatProviderBackend",
    "Clock",
    "CoalesceOutcome",
    "CoalescingTable",
    "EchoBackend",
    "FairShareConfig",
    "FairShareScheduler",
    "InferenceBackend",
    "InferenceRequest",
    "InferenceResult",
    "InferenceRouter",
    "LoadSnapshot",
    "MultiModelRouter",
    "NoEligibleWorker",
    "P2Quantile",
    "PackResult",
    "PlannerConfig",
    "PrefixCacheOracle",
    "QueueTimeSLA",
    "QueueTimeSLAExpired",
    "RejectReason",
    "RequestPriority",
    "RequestState",
    "ResidencyOracle",
    "RouterConfig",
    "RouterConfigError",
    "RouterError",
    "RouterSimulator",
    "RouterStats",
    "ScenarioConfig",
    "SimBackend",
    "SimReport",
    "TenantSpec",
    "TokenBinPacker",
    "TransitionHook",
    "VirtualClock",
    "Worker",
    "WorkerConfig",
    "WorkerPool",
    "WorkerPoolController",
    "WorkerResolver",
    "WorkerScore",
    "WorkerView",
    "build_multi_model_router",
    "build_router",
    "prefix_key_for",
    "total_tokens",
]
__all__: list[str] = []
