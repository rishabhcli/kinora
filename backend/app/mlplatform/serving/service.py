"""The serving-platform façade — one object that wires registry + distillation + sim.

:class:`ServingPlatform` is the single entry point a caller (a CLI, a future API
route, or the composition root) talks to. It owns:

* a :class:`ModelRegistry` (seeded with the default Kinora catalog by default),
* a :class:`DistillationPipeline` over that registry,
* a :class:`DatasetSource` (facet A) and :class:`RewardModel` (facet B) — injected,
  defaulting to the offline fakes so the platform is fully usable with no siblings,
* and the serving :class:`ServingSimulator` / :class:`CapacityPlanner`.

The façade composes the lifecycle into the operations an operator actually performs:
``distill_and_register`` (teacher → student), ``gate`` (run the eval gate), ``promote``
/ ``rollback`` (move a version up/down the ladder, gate-checked), and ``simulate`` /
``plan_capacity`` (predict throughput/latency/cost). Nothing here is async, nothing
touches the network, nothing spends credits.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.mlplatform.serving.batching import ContinuousBatchConfig
from app.mlplatform.serving.catalog import build_default_registry
from app.mlplatform.serving.contracts import (
    Dataset,
    DatasetSource,
    HeuristicRewardModel,
    RewardModel,
    StaticDatasetSource,
    synthetic_dataset,
)
from app.mlplatform.serving.distillation import (
    DistillationPipeline,
    DistillationResult,
    DistillationSpec,
)
from app.mlplatform.serving.kvcache import PagedKVConfig
from app.mlplatform.serving.metrics import ServingReport
from app.mlplatform.serving.model import ModelVersion, Stage
from app.mlplatform.serving.planner import CapacityPlan, CapacityPlanner, PlanCandidate, SweepGrid
from app.mlplatform.serving.registry import EvalGate, GateResult, ModelRegistry
from app.mlplatform.serving.requests import InferenceRequest, WorkloadGenerator
from app.mlplatform.serving.simulator import ServingSimulator, SimConfig


def _default_datasets() -> DatasetSource:
    """Offline facet-A stand-in: a couple of synthetic suites."""
    return StaticDatasetSource(
        [
            synthetic_dataset("eval-default", size=32),
            synthetic_dataset("distill-default", size=64),
        ]
    )


class ServingPlatform:
    """The wired ML-platform serving facade.

    Construct with defaults for a fully offline, ready-to-use platform::

        platform = ServingPlatform()
        result = platform.distill_and_register(spec, dataset_name="distill-default")
        platform.gate(result.student.ref, dataset_name="eval-default")
        platform.promote(result.student.ref, Stage.PROD)
        report = platform.simulate(result.student.ref)
    """

    def __init__(
        self,
        *,
        registry: ModelRegistry | None = None,
        datasets: DatasetSource | None = None,
        reward: RewardModel | None = None,
        gate: EvalGate | None = None,
    ) -> None:
        self.registry = registry if registry is not None else build_default_registry()
        self.datasets = datasets if datasets is not None else _default_datasets()
        self.reward = reward if reward is not None else HeuristicRewardModel()
        self.gate = gate if gate is not None else EvalGate()
        self.distiller = DistillationPipeline(self.registry)

    # -- datasets ---------------------------------------------------------- #

    def dataset(self, name: str) -> Dataset:
        """Resolve a dataset by name from the configured source (facet A)."""
        return self.datasets.get(name)

    # -- distillation ------------------------------------------------------ #

    def distill_and_register(
        self, spec: DistillationSpec, *, dataset_name: str
    ) -> DistillationResult:
        """Distil a student from its teacher over a named corpus and register it."""
        corpus = self.dataset(dataset_name)
        return self.distiller.distill(spec, corpus, reward=self.reward, register=True)

    # -- eval gate --------------------------------------------------------- #

    def gate_model(
        self, ref: str, *, dataset_name: str, gate: EvalGate | None = None
    ) -> GateResult:
        """Run the eval gate for a model version over a named eval dataset."""
        eval_ds = self.dataset(dataset_name)
        return self.registry.run_gate(ref, gate or self.gate, eval_ds, self.reward)

    # -- promotion --------------------------------------------------------- #

    def promote(self, ref: str, target: Stage | None = None) -> ModelVersion:
        """Promote a version one rung, or all the way to ``target`` if given."""
        if target is None:
            return self.registry.promote(ref)
        return self.registry.promote_to(ref, target)

    def rollback(self, ref: str) -> ModelVersion:
        """Roll a version down one rung of the ladder."""
        return self.registry.rollback(ref)

    def production(self, name: str) -> ModelVersion | None:
        """The current production version of a model name (or ``None``)."""
        return self.registry.production(name)

    # -- simulation -------------------------------------------------------- #

    def simulate(
        self,
        ref: str,
        *,
        workload: Sequence[InferenceRequest] | None = None,
        cache: PagedKVConfig | None = None,
        batch: ContinuousBatchConfig | None = None,
        shared_prefix_key: str | None = None,
    ) -> ServingReport:
        """Simulate serving a registered model version under a workload.

        Uses the version's own serving profile and a sensible default workload/config
        when none is supplied, so a single ``ref`` is enough to get a report.
        """
        version = self.registry.get(ref)
        reqs = list(workload) if workload is not None else self.default_workload()
        cfg = SimConfig(
            profile=version.profile,
            cache=cache or PagedKVConfig(total_blocks=1024, block_tokens=16),
            batch=batch
            or ContinuousBatchConfig(
                max_batch_size=16, max_batch_tokens=8192, max_admit_per_step=8
            ),
            shared_prefix_key=shared_prefix_key,
        )
        return ServingSimulator(cfg).run(reqs)

    def plan_capacity(
        self,
        ref: str,
        *,
        workload: Sequence[InferenceRequest] | None = None,
        grid: SweepGrid | None = None,
        objective: str = "tokens_per_s",
    ) -> CapacityPlan:
        """Sweep serving configs for a model version and rank them by ``objective``."""
        version = self.registry.get(ref)
        reqs = list(workload) if workload is not None else self.default_workload()
        return CapacityPlanner(version.profile).sweep(reqs, grid, objective=objective)

    def recommend_config(
        self,
        ref: str,
        *,
        p99_latency_slo_ms: float,
        workload: Sequence[InferenceRequest] | None = None,
        grid: SweepGrid | None = None,
    ) -> PlanCandidate:
        """Recommend the cheapest serving config meeting a p99 latency SLO."""
        version = self.registry.get(ref)
        reqs = list(workload) if workload is not None else self.default_workload()
        return CapacityPlanner(version.profile).recommend(
            reqs, p99_latency_slo_ms=p99_latency_slo_ms, grid=grid
        )

    @staticmethod
    def default_workload(*, seed: str = "platform", n: int = 64) -> list[InferenceRequest]:
        """A deterministic Kinora-shaped read-ahead workload for simulation."""
        return WorkloadGenerator(
            seed=seed,
            n_requests=n,
            mean_prompt_tokens=512,
            prompt_spread=192,
            mean_gen_tokens=96,
            gen_spread=48,
            max_tokens=192,
        ).generate()


__all__ = ["ServingPlatform"]
