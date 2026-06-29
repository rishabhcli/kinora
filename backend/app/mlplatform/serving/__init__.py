"""ML-platform serving facet — registry, distillation, and the serving simulator.

This subpackage owns three things, all pure and offline:

1. **Model registry** (:mod:`registry`) — versions, lineage, eval gates, a staged
   promotion ladder (``dev → staging → canary → prod``) with rollback. Eval gates
   consume facet A datasets + facet B reward models through the contracts in
   :mod:`contracts`.
2. **Knowledge distillation** (:mod:`distillation`) — teacher→student dataset
   generation and a *simulated* training-orchestration loop that produces a new
   student model version (with lineage back to its teacher) ready to register.
3. **Serving simulation** — a deterministic discrete-event model of an LLM
   inference server: continuous batching (:mod:`batching`), a paged KV-cache with
   block reuse (:mod:`kvcache`), speculative decoding (:mod:`speculative`), and the
   event loop that ties them together (:mod:`simulator`) to predict
   throughput / latency / cost under load (:mod:`metrics`).

The "GPU" is a clock-driven simulation, not a device. There are no live calls and
no credits — ``KINORA_LIVE_VIDEO`` stays OFF throughout.
"""

from __future__ import annotations

from app.mlplatform.serving.batching import (
    BatchScheduler,
    ContinuousBatchConfig,
)
from app.mlplatform.serving.catalog import DEFAULT_CATALOG, build_default_registry
from app.mlplatform.serving.contracts import (
    Dataset,
    DatasetCase,
    DatasetSource,
    HeuristicRewardModel,
    RewardModel,
    RewardScore,
    StaticDatasetSource,
    synthetic_dataset,
)
from app.mlplatform.serving.distillation import (
    DistillationPipeline,
    DistillationResult,
    DistillationSpec,
    TeacherStudentExample,
)
from app.mlplatform.serving.errors import (
    CapacityError,
    DistillationError,
    InvariantViolationError,
    MLPlatformError,
    ModelNotFoundError,
    PromotionError,
    RegistryError,
    ServingConfigError,
)
from app.mlplatform.serving.kvcache import PagedKVCache, PagedKVConfig
from app.mlplatform.serving.metrics import ServingReport, summarize_run
from app.mlplatform.serving.model import (
    ModelKind,
    ModelProfile,
    ModelVersion,
    Stage,
)
from app.mlplatform.serving.planner import (
    CapacityPlan,
    CapacityPlanner,
    PlanCandidate,
    SweepGrid,
)
from app.mlplatform.serving.registry import EvalGate, GateResult, ModelRegistry
from app.mlplatform.serving.requests import (
    InferenceRequest,
    RequestState,
    WorkloadGenerator,
)
from app.mlplatform.serving.service import ServingPlatform
from app.mlplatform.serving.simulator import ServingSimulator, SimConfig
from app.mlplatform.serving.speculative import SpeculativeConfig, SpeculativeDecoder

__all__ = [
    "DEFAULT_CATALOG",
    "BatchScheduler",
    "CapacityError",
    "CapacityPlan",
    "CapacityPlanner",
    "ContinuousBatchConfig",
    "Dataset",
    "DatasetCase",
    "DatasetSource",
    "DistillationError",
    "DistillationPipeline",
    "DistillationResult",
    "DistillationSpec",
    "EvalGate",
    "GateResult",
    "HeuristicRewardModel",
    "InferenceRequest",
    "InvariantViolationError",
    "MLPlatformError",
    "ModelKind",
    "ModelNotFoundError",
    "ModelProfile",
    "ModelRegistry",
    "ModelVersion",
    "PagedKVCache",
    "PagedKVConfig",
    "PlanCandidate",
    "PromotionError",
    "RegistryError",
    "RequestState",
    "RewardModel",
    "RewardScore",
    "ServingConfigError",
    "ServingPlatform",
    "ServingReport",
    "ServingSimulator",
    "SimConfig",
    "SpeculativeConfig",
    "SpeculativeDecoder",
    "Stage",
    "StaticDatasetSource",
    "SweepGrid",
    "TeacherStudentExample",
    "WorkloadGenerator",
    "build_default_registry",
    "summarize_run",
    "synthetic_dataset",
]
