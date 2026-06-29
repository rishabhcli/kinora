"""Experiment tracking for alignment runs — params, metrics, artifacts, lineage.

A tiny, dependency-free MLflow-shaped tracker: an :class:`Experiment` groups
:class:`Run`s; each run logs hyper-parameters, time-series metrics, tagged
artifacts (the serialized reward model / DPO policy dicts), and a parent link so a
fine-tuning job's lineage is reconstructable. Everything is in-memory and
deterministic — the FT orchestrator writes here, and the A/B harness reads the
"best run per metric" out.

This is the **experiment-tracking** half of the orchestrator requirement; the
job lifecycle lives in ``orchestrator.py`` and writes its results here.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from .errors import ExperimentError


@dataclass
class Run:
    """One tracked run: params in, metrics + artifacts out, with lineage.

    ``metrics`` maps a metric name to a step-ordered list of ``(step, value)`` so a
    training curve (loss per epoch, reward per sweep step) is recoverable.
    ``status`` follows the run lifecycle; ``parent_run_id`` links a child (e.g. an
    evaluation run) to the training run it derives from.
    """

    run_id: str
    experiment: str
    params: dict[str, object] = field(default_factory=dict)
    metrics: dict[str, list[tuple[int, float]]] = field(default_factory=dict)
    artifacts: dict[str, object] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    status: str = "running"
    parent_run_id: str | None = None
    created_at: float = field(default_factory=time.time)
    ended_at: float | None = None

    def log_param(self, key: str, value: object) -> None:
        if key in self.params and self.params[key] != value:
            raise ExperimentError(f"param {key!r} already logged with a different value")
        self.params[key] = value

    def log_params(self, params: dict[str, object]) -> None:
        for k, v in params.items():
            self.log_param(k, v)

    def log_metric(self, key: str, value: float, *, step: int = 0) -> None:
        self.metrics.setdefault(key, []).append((step, float(value)))

    def log_artifact(self, key: str, value: object) -> None:
        self.artifacts[key] = value

    def set_tag(self, key: str, value: str) -> None:
        self.tags[key] = value

    def last_metric(self, key: str) -> float:
        series = self.metrics.get(key)
        if not series:
            raise ExperimentError(f"no metric {key!r} on run {self.run_id}")
        return series[-1][1]

    def best_metric(self, key: str, *, maximize: bool = True) -> float:
        series = self.metrics.get(key)
        if not series:
            raise ExperimentError(f"no metric {key!r} on run {self.run_id}")
        vals = [v for _, v in series]
        return max(vals) if maximize else min(vals)

    def finish(self, status: str = "finished") -> None:
        self.status = status
        self.ended_at = time.time()


@dataclass
class Experiment:
    """A named container of runs."""

    name: str
    runs: dict[str, Run] = field(default_factory=dict)

    def __iter__(self) -> Iterator[Run]:
        return iter(self.runs.values())

    def __len__(self) -> int:
        return len(self.runs)


@dataclass
class ExperimentTracker:
    """In-memory experiment / run store with a query API.

    Run ids are deterministic (``<experiment>-<seq>``) so a replayed pipeline
    produces identical lineage. The tracker is the single source of truth the
    orchestrator and A/B harness share.
    """

    experiments: dict[str, Experiment] = field(default_factory=dict)
    _seq: int = 0

    def get_or_create_experiment(self, name: str) -> Experiment:
        if name not in self.experiments:
            self.experiments[name] = Experiment(name=name)
        return self.experiments[name]

    def start_run(
        self,
        experiment: str,
        *,
        params: dict[str, object] | None = None,
        parent_run_id: str | None = None,
        tags: dict[str, str] | None = None,
        run_id: str | None = None,
    ) -> Run:
        exp = self.get_or_create_experiment(experiment)
        if run_id is None:
            self._seq += 1
            run_id = f"{experiment}-{self._seq:04d}"
        if run_id in exp.runs:
            raise ExperimentError(f"run {run_id!r} already exists in {experiment!r}")
        if parent_run_id is not None and not self._find_run(parent_run_id):
            raise ExperimentError(f"parent run {parent_run_id!r} not found")
        run = Run(
            run_id=run_id,
            experiment=experiment,
            params=dict(params or {}),
            parent_run_id=parent_run_id,
            tags=dict(tags or {}),
        )
        exp.runs[run_id] = run
        return run

    def get_run(self, run_id: str) -> Run:
        run = self._find_run(run_id)
        if run is None:
            raise ExperimentError(f"run {run_id!r} not found")
        return run

    def _find_run(self, run_id: str) -> Run | None:
        for exp in self.experiments.values():
            if run_id in exp.runs:
                return exp.runs[run_id]
        return None

    def best_run(
        self, experiment: str, metric: str, *, maximize: bool = True
    ) -> Run:
        """The run in ``experiment`` with the best (last) value of ``metric``."""

        exp = self.experiments.get(experiment)
        if exp is None or not exp.runs:
            raise ExperimentError(f"experiment {experiment!r} has no runs")
        candidates = [r for r in exp.runs.values() if metric in r.metrics]
        if not candidates:
            raise ExperimentError(f"no run in {experiment!r} logged metric {metric!r}")
        key = (lambda r: r.last_metric(metric)) if maximize else (
            lambda r: -r.last_metric(metric)
        )
        return max(candidates, key=key)

    def children(self, run_id: str) -> list[Run]:
        """All runs whose ``parent_run_id`` is ``run_id`` (lineage descent)."""

        out: list[Run] = []
        for exp in self.experiments.values():
            out.extend(r for r in exp.runs.values() if r.parent_run_id == run_id)
        return out

    def query(
        self,
        *,
        experiment: str | None = None,
        status: str | None = None,
        tag: tuple[str, str] | None = None,
    ) -> list[Run]:
        """Filter runs by experiment / status / tag."""

        runs: list[Run] = []
        exps = (
            [self.experiments[experiment]]
            if experiment is not None and experiment in self.experiments
            else list(self.experiments.values())
        )
        for exp in exps:
            for r in exp.runs.values():
                if status is not None and r.status != status:
                    continue
                if tag is not None and r.tags.get(tag[0]) != tag[1]:
                    continue
                runs.append(r)
        return runs
