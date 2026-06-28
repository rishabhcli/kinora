"""Kinora deployment orchestration service (kinora.md §12, §12.6).

The §12.6 *proof-of-deployment* worker (``deploy/alibaba_render_worker.py``)
shows Kinora running on Alibaba Cloud (OSS + DashScope + ECS/FC). This package
is the **orchestration layer around that worker**: how a new build of the
backend image (api / ingest-worker / render-worker / mcp / frontend — the §0
process model) is *safely promoted* into an environment.

It is deliberately **cloud-agnostic and pure**. Every effectful seam — health
probing, traffic shifting, smoke tests, draining the §12.1 render queue,
hydrating secrets — is a small typed Protocol that the production wiring fills
with a real Alibaba/SLB/Tair adapter and the tests fill with an in-memory fake.
Nothing here imports ``oss2``, ``dashscope``, ``boto3``, or any cloud SDK, so
the whole rollout/rollback decision logic is unit-testable with **zero credits
and zero network** — exactly the constraint the §12.6 worker lives under
(``KINORA_LIVE_VIDEO`` OFF).

Public surface (import from ``deploy.orchestrator``):

* :mod:`models` — frozen value types and the deploy state machine.
* :mod:`health` — readiness/liveness probing with stability windows.
* :mod:`strategies` — blue-green and canary rollout planners.
* :mod:`slo` — SLO evaluation + breach detection driving auto-rollback.
* :mod:`audit` — append-only deploy audit trail.
* :mod:`promotion` — artifact promotion across an environment pipeline.
* :mod:`drain` — graceful drain + shutdown coordination with queue workers.
* :mod:`hydration` — config/secret hydration with redaction.
* :mod:`smoke` — smoke-test gating before traffic shifts.
* :mod:`seams` — the provision/traffic effectful Protocols.
* :mod:`orchestrator` — the :class:`DeploymentOrchestrator` state machine.
* :mod:`fakes` — in-memory doubles for every seam (cloud-free).
* :mod:`simulator` — a deterministic, cloud-free rollout simulator.
"""

from __future__ import annotations

from deploy.orchestrator.audit import AuditSink, AuditTrail, InMemoryAuditSink
from deploy.orchestrator.drain import (
    DrainCoordinator,
    DrainPhase,
    DrainResult,
    DrainTarget,
)
from deploy.orchestrator.health import (
    HealthGate,
    HealthProbe,
    ProbeResult,
    StabilityWindow,
    quorum_status,
)
from deploy.orchestrator.hydration import (
    HydratedConfig,
    HydrationError,
    Hydrator,
    redact_value,
)
from deploy.orchestrator.models import (
    ABORTABLE_STATES,
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    Artifact,
    DeployEvent,
    DeployState,
    Environment,
    HealthStatus,
    RolloutStrategy,
    ServiceRole,
    SLOResult,
    SLOTarget,
    can_transition,
)
from deploy.orchestrator.orchestrator import (
    DeploymentOrchestrator,
    DeploymentResult,
    OrchestratorConfig,
    RollbackError,
    StateTransitionError,
    StepOutcome,
)
from deploy.orchestrator.promotion import PromotionPipeline, PromotionRejectedError
from deploy.orchestrator.seams import Provisioner, TrafficRouter
from deploy.orchestrator.slo import DEFAULT_RENDER_SLOS, MetricSource, SLOEvaluator
from deploy.orchestrator.smoke import (
    SmokeCheck,
    SmokeGate,
    SmokeOutcome,
    SmokeReport,
)
from deploy.orchestrator.strategies import (
    BlueGreenStrategy,
    CanaryStep,
    CanaryStrategy,
    RecreateStrategy,
    RolloutPlan,
    RolloutStep,
    Strategy,
    strategy_for,
)

__all__ = [
    "ABORTABLE_STATES",
    "Artifact",
    "AuditSink",
    "AuditTrail",
    "BlueGreenStrategy",
    "CanaryStep",
    "CanaryStrategy",
    "DEFAULT_RENDER_SLOS",
    "DeployEvent",
    "DeployState",
    "DeploymentOrchestrator",
    "DeploymentResult",
    "DrainCoordinator",
    "DrainPhase",
    "DrainResult",
    "DrainTarget",
    "Environment",
    "HealthGate",
    "HealthProbe",
    "HealthStatus",
    "HydratedConfig",
    "HydrationError",
    "Hydrator",
    "InMemoryAuditSink",
    "LEGAL_TRANSITIONS",
    "MetricSource",
    "OrchestratorConfig",
    "ProbeResult",
    "PromotionPipeline",
    "PromotionRejectedError",
    "Provisioner",
    "RecreateStrategy",
    "RollbackError",
    "RolloutPlan",
    "RolloutStep",
    "RolloutStrategy",
    "SLOEvaluator",
    "SLOResult",
    "SLOTarget",
    "ServiceRole",
    "SmokeCheck",
    "SmokeGate",
    "SmokeOutcome",
    "SmokeReport",
    "StabilityWindow",
    "StateTransitionError",
    "StepOutcome",
    "Strategy",
    "TERMINAL_STATES",
    "TrafficRouter",
    "can_transition",
    "quorum_status",
    "redact_value",
    "strategy_for",
]
