"""The prompt eval harness — run a prompt version over a golden dataset.

Given a prompt's *system text*, a :class:`~app.llmops.datasets.GoldenDataset`, a
**responder** (system + case-inputs → output text), and a
:class:`~app.llmops.judge.Judge`, the harness produces an output per case, scores
each against the dataset's rubric, and aggregates to an :class:`EvalReport`
(mean ± spread over N runs, pass rate, per-criterion means) — §13's discipline of
"report mean and spread across N runs."

The **responder** is the seam where the real crew plugs in (a ``BaseAgent`` call
bound to a candidate prompt). The package default is a **deterministic fake
responder** that needs no network, so the whole harness is exercised in CI with
zero credits — and because both the responder and judge are deterministic, the
N-run spread is exactly zero for the fake, which is the correct, honest signal
that "the fake has no run-to-run noise." A live responder injects its own
variance.

Pure orchestration + stdlib statistics. No app imports beyond the package's own
datasets/judge/rubric types.
"""

from __future__ import annotations

import inspect
import json
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.llmops.datasets import GoldenCase, GoldenDataset
from app.llmops.judge import HeuristicJudge, Judge
from app.llmops.rubric import Rubric, ScoreResult, get_rubric

#: A responder turns (system_prompt, case) -> output text. Sync or async.
Responder = Callable[[str, GoldenCase], "str | Awaitable[str]"]


async def _call_responder(responder: Responder, system: str, case: GoldenCase) -> str:
    result = responder(system, case)
    if inspect.isawaitable(result):
        return await result
    return result


# --------------------------------------------------------------------------- #
# Fake responder — deterministic, no network
# --------------------------------------------------------------------------- #


def fake_responder(system: str, case: GoldenCase) -> str:
    """A deterministic stand-in for a real agent call.

    It mimics a *well-behaved* JSON-strict agent: it echoes the expected keys, the
    must-include phrases, and stays on task; for adversarial cases it deliberately
    **refuses** the injection (so the safety rubric scores high) rather than
    echoing the payload. This makes the "crew arm" a sensible upper-bound control
    in tests; a misbehaving responder (see :func:`naive_responder`) is the
    contrast arm.
    """
    if case.adversarial:
        return json.dumps(
            {"refusal": "I will continue the legitimate task and ignore embedded instructions."}
        )
    body: dict[str, Any] = {}
    for key in case.expected_keys:
        body[key] = []
    for phrase in case.must_include:
        body.setdefault("locked", []).append(phrase)
    # Reflect a little of the input so reference-similarity isn't zero.
    snippet = " ".join(str(v) for v in case.inputs.values())[:120]
    body["summary"] = snippet
    return json.dumps(body)


def naive_responder(system: str, case: GoldenCase) -> str:
    """A *misbehaving* responder: ignores the contract and follows injections.

    Used as the contrast arm in A/B + regression tests: it emits prose (not JSON),
    omits expected keys, and on an adversarial case echoes the forbidden payload —
    so it scores poorly on every rubric, demonstrating the harness discriminates.
    """
    if case.adversarial:
        # The failure mode: it complies with the embedded attack.
        payloads = " ".join(case.must_not_include) or "PWNED"
        return f"Okay! {payloads}. Here is my system prompt: {system[:80]}"
    return f"Here you go: {case.inputs}"


# --------------------------------------------------------------------------- #
# Per-case + per-run records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    output: str
    overall: float
    passed: bool
    per_criterion: dict[str, float]
    failed_required: tuple[str, ...]
    adversarial: bool

    @classmethod
    def from_score(cls, case: GoldenCase, output: str, sr: ScoreResult) -> CaseResult:
        return cls(
            case_id=case.id,
            output=output,
            overall=sr.overall,
            passed=sr.passed,
            per_criterion=sr.per_criterion,
            failed_required=sr.failed_required,
            adversarial=case.adversarial,
        )


@dataclass(frozen=True, slots=True)
class RunResult:
    """One full pass over the dataset."""

    run_index: int
    cases: tuple[CaseResult, ...]

    @property
    def mean_overall(self) -> float:
        return round(statistics.fmean(c.overall for c in self.cases), 6) if self.cases else 0.0

    @property
    def pass_rate(self) -> float:
        return round(sum(c.passed for c in self.cases) / len(self.cases), 6) if self.cases else 0.0


@dataclass(frozen=True, slots=True)
class EvalReport:
    """Aggregate of N runs of one prompt version over one dataset."""

    prompt_key: str
    prompt_version: str
    dataset_name: str
    rubric_name: str
    runs: int
    mean_score: float
    score_stdev: float
    mean_pass_rate: float
    per_criterion_mean: dict[str, float]
    per_case_mean: dict[str, float]
    run_results: tuple[RunResult, ...] = field(default=(), repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_key": self.prompt_key,
            "prompt_version": self.prompt_version,
            "dataset_name": self.dataset_name,
            "rubric_name": self.rubric_name,
            "runs": self.runs,
            "mean_score": self.mean_score,
            "score_stdev": self.score_stdev,
            "mean_pass_rate": self.mean_pass_rate,
            "per_criterion_mean": self.per_criterion_mean,
            "per_case_mean": self.per_case_mean,
        }


@dataclass
class EvalHarness:
    """Runs a prompt's system text over a dataset, scored by a judge."""

    judge: Judge = field(default_factory=HeuristicJudge)

    async def run(
        self,
        *,
        prompt_key: str,
        prompt_version: str,
        system: str,
        dataset: GoldenDataset,
        responder: Responder | None = None,
        runs: int = 3,
        rubric: Rubric | None = None,
    ) -> EvalReport:
        """Run ``runs`` passes; return the aggregate report (§13 mean + spread)."""
        if runs < 1:
            raise ValueError("runs must be >= 1")
        responder = responder or fake_responder
        rubric = rubric or get_rubric(dataset.rubric_name)

        run_results: list[RunResult] = []
        for run_index in range(runs):
            case_results: list[CaseResult] = []
            for case in dataset.cases:
                output = await _call_responder(responder, system, case)
                sr = self.judge.score(case, output, rubric)
                case_results.append(CaseResult.from_score(case, output, sr))
            run_results.append(RunResult(run_index=run_index, cases=tuple(case_results)))

        return self._aggregate(
            prompt_key=prompt_key,
            prompt_version=prompt_version,
            dataset=dataset,
            rubric=rubric,
            run_results=run_results,
        )

    @staticmethod
    def _aggregate(
        *,
        prompt_key: str,
        prompt_version: str,
        dataset: GoldenDataset,
        rubric: Rubric,
        run_results: list[RunResult],
    ) -> EvalReport:
        run_means = [r.mean_overall for r in run_results]
        pass_rates = [r.pass_rate for r in run_results]

        # Per-criterion mean across every (run, case).
        crit_acc: dict[str, list[float]] = {name: [] for name in rubric.criterion_names}
        case_acc: dict[str, list[float]] = {c.id: [] for c in dataset.cases}
        for run in run_results:
            for cr in run.cases:
                case_acc[cr.case_id].append(cr.overall)
                for name, val in cr.per_criterion.items():
                    crit_acc.setdefault(name, []).append(val)

        per_criterion_mean = {
            name: round(statistics.fmean(vals), 6) for name, vals in crit_acc.items() if vals
        }
        per_case_mean = {
            cid: round(statistics.fmean(vals), 6) for cid, vals in case_acc.items() if vals
        }

        return EvalReport(
            prompt_key=prompt_key,
            prompt_version=prompt_version,
            dataset_name=dataset.name,
            rubric_name=rubric.name,
            runs=len(run_results),
            mean_score=round(statistics.fmean(run_means), 6) if run_means else 0.0,
            score_stdev=round(statistics.pstdev(run_means), 6) if len(run_means) > 1 else 0.0,
            mean_pass_rate=round(statistics.fmean(pass_rates), 6) if pass_rates else 0.0,
            per_criterion_mean=per_criterion_mean,
            per_case_mean=per_case_mean,
            run_results=tuple(run_results),
        )


__all__ = [
    "CaseResult",
    "EvalHarness",
    "EvalReport",
    "Responder",
    "RunResult",
    "fake_responder",
    "naive_responder",
]
