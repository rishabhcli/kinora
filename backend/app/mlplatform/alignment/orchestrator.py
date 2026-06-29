"""Provider-abstracted fine-tuning-job orchestrator (faked executor, §11 budget-safe).

The orchestrator turns "train a director-aligned reward model / preference policy"
into a managed *job* with a real lifecycle, a pluggable *provider* backend, and
full experiment tracking — without ever calling a live model or spending a credit.
That last constraint is structural, not incidental: the executor is an injected
:class:`FineTuneExecutor`, and the only executor this package ships is the
**local in-process** one that runs the pure-NumPy trainers from this very package.
A hosted DashScope / OpenAI executor would implement the same protocol, but is
*not* shipped here (``KINORA_LIVE_VIDEO`` OFF, zero credits).

Lifecycle (a strict state machine — illegal transitions raise):

    PENDING → RUNNING → SUCCEEDED
                      ↘ FAILED
    PENDING → CANCELLED      (cancel before start)
    RUNNING → CANCELLED      (cooperative cancel)

Each job:

* validates its :class:`FineTuneSpec` (provider known, data present, objective in
  the supported set);
* opens an :class:`~app.mlplatform.alignment.experiments.Run` and logs params;
* hands the spec to the provider's executor, which produces a model artifact +
  training metrics (deterministically);
* logs metrics + the serialized artifact, then transitions to SUCCEEDED.

The orchestrator is the operational seam the rest of Kinora would call to "learn
from this week's director feedback"; everything under it is exhaustively tested
with the faked executor.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from .dpo import DPOConfig, DPOTrainer
from .errors import DataError, OrchestrationError
from .experiments import ExperimentTracker
from .reward_model import RewardModelTrainer
from .types import PreferenceDataset, SampleDataset, as_sample_dataset


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: The objectives the orchestrator knows how to fine-tune.
OBJ_REWARD = "reward"
OBJ_REWARD_PAIRWISE = "reward_pairwise"
OBJ_REWARD_COMBINED = "reward_combined"
OBJ_DPO = "dpo"
OBJECTIVES: tuple[str, ...] = (
    OBJ_REWARD,
    OBJ_REWARD_PAIRWISE,
    OBJ_REWARD_COMBINED,
    OBJ_DPO,
)


@dataclass(frozen=True)
class FineTuneSpec:
    """A declarative fine-tuning request.

    ``provider`` selects the executor backend; ``objective`` selects the trainer.
    ``samples`` / ``pairs`` carry the (already-prepared, facet-A-shaped) data; the
    objective dictates which are required. ``hyperparams`` is a free-form mapping
    the executor interprets (e.g. ``l2``, ``beta``, ``steps``).
    """

    name: str
    provider: str
    objective: str
    samples: SampleDataset | None = None
    pairs: PreferenceDataset | None = None
    hyperparams: Mapping[str, object] = field(default_factory=dict)
    base_model: str | None = None
    tags: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise DataError("FineTuneSpec.name must be non-empty")
        if self.objective not in OBJECTIVES:
            raise DataError(
                f"unknown objective {self.objective!r}; expected one of {OBJECTIVES}"
            )
        needs_samples = self.objective in (OBJ_REWARD, OBJ_REWARD_COMBINED)
        needs_pairs = self.objective in (
            OBJ_REWARD_PAIRWISE,
            OBJ_REWARD_COMBINED,
            OBJ_DPO,
        )
        if needs_samples and self.samples is None:
            raise DataError(f"objective {self.objective!r} requires `samples`")
        if needs_pairs and self.pairs is None:
            raise DataError(f"objective {self.objective!r} requires `pairs`")


@dataclass(frozen=True)
class FineTuneResult:
    """The artifact + metrics a successful fine-tune produced.

    ``artifact`` is the serialized model dict (a reward model or DPO policy), tagged
    by ``artifact_kind``. ``metrics`` is the training summary the run also logs.
    """

    artifact_kind: str
    artifact: dict[str, object]
    metrics: dict[str, float]


class FineTuneExecutor(Protocol):
    """The provider seam: turn a validated spec into a result. Injectable.

    A hosted provider would POST to DashScope / OpenAI and poll; the shipped
    :class:`LocalExecutor` runs this package's pure trainers in-process so the
    whole platform is exercisable offline with zero credits.
    """

    name: str

    def execute(self, spec: FineTuneSpec) -> FineTuneResult: ...


@dataclass
class LocalExecutor:
    """The in-process, pure-NumPy executor — the only one shipped (zero credits).

    Runs the reward-model / DPO trainers from this package. Deterministic: the
    same spec yields the same artifact, so a re-run reproduces a job exactly.
    """

    name: str = "local"

    @staticmethod
    def _hp_float(hp: Mapping[str, object], key: str, default: float) -> float:
        v = hp.get(key, default)
        return float(v)  # type: ignore[arg-type]

    @staticmethod
    def _hp_int(hp: Mapping[str, object], key: str, default: int) -> int:
        v = hp.get(key, default)
        return int(v)  # type: ignore[call-overload]

    def execute(self, spec: FineTuneSpec) -> FineTuneResult:
        hp = dict(spec.hyperparams)
        if spec.objective == OBJ_REWARD:
            trainer = RewardModelTrainer(
                l2=self._hp_float(hp, "l2", 1.0),
                max_iter=self._hp_int(hp, "max_iter", 100),
            )
            model = trainer.fit(as_sample_dataset(spec.samples))
            metrics_obj = model.evaluate(samples=spec.samples)
            return FineTuneResult(
                artifact_kind="reward_model",
                artifact=model.to_dict(),
                metrics={
                    "accuracy": metrics_obj.accuracy,
                    "auc": metrics_obj.auc,
                    "ece": metrics_obj.ece,
                    "log_loss": metrics_obj.log_loss,
                },
            )
        if spec.objective == OBJ_REWARD_PAIRWISE:
            assert spec.pairs is not None  # validated by FineTuneSpec
            trainer = RewardModelTrainer(l2=self._hp_float(hp, "l2", 1.0))
            model = trainer.fit_pairwise(spec.pairs)
            metrics_obj = model.evaluate(pairs=spec.pairs)
            return FineTuneResult(
                artifact_kind="reward_model",
                artifact=model.to_dict(),
                metrics={"pair_accuracy": metrics_obj.pair_accuracy},
            )
        if spec.objective == OBJ_REWARD_COMBINED:
            assert spec.pairs is not None  # validated by FineTuneSpec
            trainer = RewardModelTrainer(
                l2=self._hp_float(hp, "l2", 1.0),
                pairwise_weight=self._hp_float(hp, "pairwise_weight", 1.0),
            )
            model = trainer.fit_combined(as_sample_dataset(spec.samples), spec.pairs)
            metrics_obj = model.evaluate(samples=spec.samples, pairs=spec.pairs)
            return FineTuneResult(
                artifact_kind="reward_model",
                artifact=model.to_dict(),
                metrics={
                    "accuracy": metrics_obj.accuracy,
                    "pair_accuracy": metrics_obj.pair_accuracy,
                    "ece": metrics_obj.ece,
                },
            )
        if spec.objective == OBJ_DPO:
            assert spec.pairs is not None  # validated by FineTuneSpec
            cfg = DPOConfig(
                beta=self._hp_float(hp, "beta", 0.1),
                lr=self._hp_float(hp, "lr", 0.5),
                steps=self._hp_int(hp, "steps", 500),
                l2=self._hp_float(hp, "l2", 0.0),
            )
            policy = DPOTrainer(cfg).fit(spec.pairs)
            from .dpo import preference_accuracy

            return FineTuneResult(
                artifact_kind="dpo_policy",
                artifact=policy.to_dict(),
                metrics={
                    "final_loss": policy.final_loss,
                    "pref_accuracy": preference_accuracy(policy, spec.pairs),
                    "converged": float(policy.converged),
                },
            )
        raise OrchestrationError(f"executor cannot handle objective {spec.objective!r}")


class _FailingExecutor:
    """Test helper exposed for completeness — always raises (FAILED path)."""

    name = "failing"

    def execute(self, spec: FineTuneSpec) -> FineTuneResult:  # pragma: no cover
        raise RuntimeError("synthetic executor failure")


@dataclass
class FineTuneJob:
    """A tracked fine-tuning job with a strict lifecycle.

    The orchestrator owns these; callers observe ``status`` / ``result`` / ``error``
    and the linked ``run_id``. Transitions go through :meth:`_transition`, which
    rejects illegal moves (e.g. SUCCEEDED → RUNNING).
    """

    job_id: str
    spec: FineTuneSpec
    run_id: str
    status: JobStatus = JobStatus.PENDING
    result: FineTuneResult | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None

    _LEGAL: dict[JobStatus, frozenset[JobStatus]] = field(
        default_factory=lambda: {
            JobStatus.PENDING: frozenset(
                {JobStatus.RUNNING, JobStatus.CANCELLED}
            ),
            JobStatus.RUNNING: frozenset(
                {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
            ),
            JobStatus.SUCCEEDED: frozenset(),
            JobStatus.FAILED: frozenset(),
            JobStatus.CANCELLED: frozenset(),
        },
        repr=False,
    )

    @property
    def terminal(self) -> bool:
        return self.status in (
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        )

    def _transition(self, to: JobStatus) -> None:
        if to not in self._LEGAL[self.status]:
            raise OrchestrationError(
                f"illegal job transition {self.status.value} → {to.value}"
            )
        self.status = to


@dataclass
class FineTuneOrchestrator:
    """Creates, runs, and tracks fine-tuning jobs across pluggable providers.

    Register providers up front (the shipped :class:`LocalExecutor` is auto-
    registered as ``"local"``). :meth:`submit` validates + queues a job and opens a
    tracking run; :meth:`run` drives a pending job to a terminal state through its
    provider; :meth:`cancel` cooperatively cancels a non-terminal job.
    """

    tracker: ExperimentTracker = field(default_factory=ExperimentTracker)
    providers: dict[str, FineTuneExecutor] = field(default_factory=dict)
    jobs: dict[str, FineTuneJob] = field(default_factory=dict)
    _seq: int = 0

    def __post_init__(self) -> None:
        self.providers.setdefault("local", LocalExecutor())

    def register_provider(self, executor: FineTuneExecutor) -> None:
        self.providers[executor.name] = executor

    def submit(
        self, spec: FineTuneSpec, *, experiment: str | None = None
    ) -> FineTuneJob:
        """Validate + queue a job; opens a tracking run and logs the spec params."""

        if spec.provider not in self.providers:
            raise OrchestrationError(f"unknown provider {spec.provider!r}")
        self._seq += 1
        job_id = f"ftjob-{self._seq:04d}"
        exp = experiment or f"ft-{spec.name}"
        run = self.tracker.start_run(
            exp,
            params={
                "objective": spec.objective,
                "provider": spec.provider,
                "base_model": spec.base_model,
                **{f"hp.{k}": v for k, v in spec.hyperparams.items()},
            },
            tags={"job_id": job_id, **dict(spec.tags)},
        )
        job = FineTuneJob(job_id=job_id, spec=spec, run_id=run.run_id)
        self.jobs[job_id] = job
        return job

    def run(self, job_id: str) -> FineTuneJob:
        """Drive a PENDING job to a terminal state via its provider's executor."""

        job = self._job(job_id)
        if job.status is JobStatus.CANCELLED:
            return job
        if job.status is not JobStatus.PENDING:
            raise OrchestrationError(
                f"job {job_id} is {job.status.value}, cannot run"
            )
        run = self.tracker.get_run(job.run_id)
        job._transition(JobStatus.RUNNING)
        job.started_at = time.time()
        run.set_tag("status", "running")
        executor = self.providers[job.spec.provider]
        try:
            result = executor.execute(job.spec)
        except Exception as exc:  # noqa: BLE001 - record any executor fault
            job._transition(JobStatus.FAILED)
            job.error = str(exc)
            job.ended_at = time.time()
            run.set_tag("status", "failed")
            run.finish(status="failed")
            return job
        for k, v in result.metrics.items():
            run.log_metric(k, v)
        run.log_artifact(result.artifact_kind, result.artifact)
        run.set_tag("artifact_kind", result.artifact_kind)
        job.result = result
        job._transition(JobStatus.SUCCEEDED)
        job.ended_at = time.time()
        run.finish(status="finished")
        return job

    def submit_and_run(
        self, spec: FineTuneSpec, *, experiment: str | None = None
    ) -> FineTuneJob:
        """Convenience: :meth:`submit` then :meth:`run`."""

        job = self.submit(spec, experiment=experiment)
        return self.run(job.job_id)

    def cancel(self, job_id: str) -> FineTuneJob:
        """Cooperatively cancel a non-terminal job."""

        job = self._job(job_id)
        if job.terminal:
            raise OrchestrationError(f"job {job_id} already terminal ({job.status.value})")
        job._transition(JobStatus.CANCELLED)
        job.ended_at = time.time()
        run = self.tracker.get_run(job.run_id)
        run.finish(status="cancelled")
        return job

    def _job(self, job_id: str) -> FineTuneJob:
        if job_id not in self.jobs:
            raise OrchestrationError(f"unknown job {job_id!r}")
        return self.jobs[job_id]
