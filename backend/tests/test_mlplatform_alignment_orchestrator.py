"""Tests for experiment tracking + the fine-tuning-job orchestrator (faked executor)."""

from __future__ import annotations

import numpy as np
import pytest

from app.mlplatform.alignment.errors import (
    ExperimentError,
    OrchestrationError,
)
from app.mlplatform.alignment.experiments import ExperimentTracker
from app.mlplatform.alignment.orchestrator import (
    OBJ_DPO,
    OBJ_REWARD,
    OBJ_REWARD_COMBINED,
    FineTuneOrchestrator,
    FineTuneResult,
    FineTuneSpec,
    JobStatus,
)
from app.mlplatform.alignment.reward_model import RewardModel
from app.mlplatform.alignment.types import (
    PreferenceDataset,
    PreferencePair,
    Sample,
    SampleDataset,
)


def _samples(n: int = 120, seed: int = 0) -> SampleDataset:
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n):
        x = float(rng.uniform(0, 1))
        rows.append(Sample([x, float(rng.uniform(0, 1))], 1.0 if x >= 0.5 else 0.0))
    return SampleDataset(samples=tuple(rows))


def _pairs(n: int = 40) -> PreferenceDataset:
    return PreferenceDataset(
        pairs=tuple(PreferencePair([0.9, 0.5], [0.2, 0.5]) for _ in range(n))
    )


# ---- experiment tracker --------------------------------------------------- #


def test_tracker_logs_params_metrics_artifacts() -> None:
    t = ExperimentTracker()
    run = t.start_run("exp", params={"l2": 0.1})
    run.log_metric("loss", 1.0, step=0)
    run.log_metric("loss", 0.5, step=1)
    run.log_artifact("model", {"w": [1, 2]})
    run.finish()
    assert run.last_metric("loss") == 0.5
    assert run.best_metric("loss", maximize=False) == 0.5
    assert run.status == "finished"
    assert t.get_run(run.run_id).artifacts["model"] == {"w": [1, 2]}


def test_tracker_param_conflict_raises() -> None:
    t = ExperimentTracker()
    run = t.start_run("exp", params={"l2": 0.1})
    run.log_param("l2", 0.1)  # same value ok
    with pytest.raises(ExperimentError):
        run.log_param("l2", 0.2)  # conflicting value


def test_tracker_best_run_and_lineage() -> None:
    t = ExperimentTracker()
    a = t.start_run("exp")
    a.log_metric("acc", 0.7)
    b = t.start_run("exp")
    b.log_metric("acc", 0.9)
    best = t.best_run("exp", "acc", maximize=True)
    assert best.run_id == b.run_id
    child = t.start_run("exp", parent_run_id=b.run_id)
    assert t.children(b.run_id) == [child]


def test_tracker_query_filters() -> None:
    t = ExperimentTracker()
    r1 = t.start_run("e1", tags={"kind": "reward"})
    r1.finish()
    t.start_run("e2", tags={"kind": "dpo"})
    finished = t.query(status="finished")
    assert [r.run_id for r in finished] == [r1.run_id]
    rewards = t.query(tag=("kind", "reward"))
    assert [r.run_id for r in rewards] == [r1.run_id]


def test_tracker_unknown_run_and_parent() -> None:
    t = ExperimentTracker()
    with pytest.raises(ExperimentError):
        t.get_run("nope")
    with pytest.raises(ExperimentError):
        t.start_run("exp", parent_run_id="nope")


# ---- spec validation ------------------------------------------------------ #


def test_spec_validates_objective_and_data() -> None:
    from app.mlplatform.alignment.errors import DataError

    with pytest.raises(DataError):
        FineTuneSpec(name="x", provider="local", objective="bogus")
    with pytest.raises(DataError):
        FineTuneSpec(name="x", provider="local", objective=OBJ_REWARD)  # no samples
    with pytest.raises(DataError):
        FineTuneSpec(name="x", provider="local", objective=OBJ_DPO)  # no pairs
    # Combined needs both.
    with pytest.raises(DataError):
        FineTuneSpec(
            name="x", provider="local", objective=OBJ_REWARD_COMBINED, samples=_samples()
        )


# ---- orchestrator happy path ---------------------------------------------- #


def test_orchestrator_runs_reward_job_to_success() -> None:
    orch = FineTuneOrchestrator()
    spec = FineTuneSpec(
        name="reward",
        provider="local",
        objective=OBJ_REWARD,
        samples=_samples(),
        hyperparams={"l2": 0.01},
    )
    job = orch.submit_and_run(spec)
    assert job.status is JobStatus.SUCCEEDED
    assert job.result is not None
    assert job.result.artifact_kind == "reward_model"
    # The artifact round-trips into a usable model.
    model = RewardModel.from_dict(job.result.artifact)
    assert model.reward([0.9, 0.5]) > model.reward([0.1, 0.5])
    # The run captured the metrics + artifact.
    run = orch.tracker.get_run(job.run_id)
    assert run.last_metric("accuracy") > 0.8
    assert "reward_model" in run.artifacts
    assert run.status == "finished"


def test_orchestrator_runs_dpo_job() -> None:
    orch = FineTuneOrchestrator()
    spec = FineTuneSpec(
        name="dpo",
        provider="local",
        objective=OBJ_DPO,
        pairs=_pairs(),
        hyperparams={"beta": 0.2, "lr": 0.4, "steps": 300},
    )
    job = orch.submit_and_run(spec)
    assert job.status is JobStatus.SUCCEEDED
    assert job.result is not None
    assert job.result.artifact_kind == "dpo_policy"
    run = orch.tracker.get_run(job.run_id)
    assert run.last_metric("pref_accuracy") >= 0.5


def test_orchestrator_is_deterministic() -> None:
    def _spec() -> FineTuneSpec:
        return FineTuneSpec(
            name="reward",
            provider="local",
            objective=OBJ_REWARD,
            samples=_samples(seed=5),
            hyperparams={"l2": 0.05},
        )

    j1 = FineTuneOrchestrator().submit_and_run(_spec())
    j2 = FineTuneOrchestrator().submit_and_run(_spec())
    assert j1.result is not None and j2.result is not None
    assert j1.result.artifact == j2.result.artifact


# ---- lifecycle + failure -------------------------------------------------- #


def test_unknown_provider_rejected() -> None:
    orch = FineTuneOrchestrator()
    with pytest.raises(OrchestrationError):
        orch.submit(
            FineTuneSpec(name="x", provider="ghost", objective=OBJ_REWARD, samples=_samples())
        )


def test_cancel_pending_job() -> None:
    orch = FineTuneOrchestrator()
    job = orch.submit(
        FineTuneSpec(name="x", provider="local", objective=OBJ_REWARD, samples=_samples())
    )
    cancelled = orch.cancel(job.job_id)
    assert cancelled.status is JobStatus.CANCELLED
    # Running a cancelled job is a no-op (stays cancelled).
    assert orch.run(job.job_id).status is JobStatus.CANCELLED
    # Cannot cancel twice.
    with pytest.raises(OrchestrationError):
        orch.cancel(job.job_id)


def test_failing_executor_marks_job_failed() -> None:
    class Boom:
        name = "boom"

        def execute(self, spec: FineTuneSpec) -> FineTuneResult:
            raise RuntimeError("kaboom")

    orch = FineTuneOrchestrator()
    orch.register_provider(Boom())
    job = orch.submit(
        FineTuneSpec(name="x", provider="boom", objective=OBJ_REWARD, samples=_samples())
    )
    out = orch.run(job.job_id)
    assert out.status is JobStatus.FAILED
    assert out.error is not None and "kaboom" in out.error
    assert orch.tracker.get_run(out.run_id).status == "failed"


def test_illegal_transition_running_already_run() -> None:
    orch = FineTuneOrchestrator()
    job = orch.submit_and_run(
        FineTuneSpec(name="x", provider="local", objective=OBJ_REWARD, samples=_samples())
    )
    # Already succeeded — re-running is illegal.
    with pytest.raises(OrchestrationError):
        orch.run(job.job_id)


def test_custom_provider_protocol() -> None:
    # A provider that returns a canned reward model — proves the seam is pluggable.
    class Canned:
        name = "canned"

        def execute(self, spec: FineTuneSpec) -> FineTuneResult:
            return FineTuneResult(
                artifact_kind="reward_model",
                artifact={
                    "weights": [0.0, 1.0],
                    "mean": [0.0],
                    "scale": [1.0],
                    "dim": 1,
                },
                metrics={"accuracy": 1.0},
            )

    orch = FineTuneOrchestrator()
    orch.register_provider(Canned())
    job = orch.submit_and_run(
        FineTuneSpec(name="c", provider="canned", objective=OBJ_REWARD, samples=_samples())
    )
    assert job.status is JobStatus.SUCCEEDED
    assert orch.tracker.get_run(job.run_id).last_metric("accuracy") == 1.0
