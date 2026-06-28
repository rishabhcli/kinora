"""Regression detection on prompt changes — does a candidate quietly get worse?

When an operator registers a new prompt version, the registry can gate it: run
the candidate's eval against a stored *baseline* report (the active version's) and
flag a **regression** if quality dropped beyond a tolerance. This is the
"don't ship a prompt that silently degrades the crew" safety net.

What counts as a regression is explicit and pre-registered (mirroring §13):

* **overall drop** — the candidate's mean score fell by more than
  ``overall_tolerance`` below the baseline;
* **pass-rate drop** — the dataset pass rate fell by more than
  ``pass_rate_tolerance``;
* **per-criterion drop** — any single rubric criterion fell by more than
  ``criterion_tolerance`` (catches "overall held but a *required* dimension
  cratered");
* **per-case regressions** — any case whose score dropped by more than
  ``case_tolerance`` (catches a localized break the mean hides).

The detector is pure: it consumes two :class:`~app.llmops.harness.EvalReport`s and
emits a verdict. It also offers :func:`detect_from_harness`, which *runs* both
reports for you given the systems + dataset.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.llmops.datasets import GoldenDataset
from app.llmops.harness import EvalHarness, EvalReport, Responder
from app.llmops.judge import Judge
from app.llmops.rubric import Rubric


class RegressionKind(StrEnum):
    OVERALL = "overall_drop"
    PASS_RATE = "pass_rate_drop"
    CRITERION = "criterion_drop"
    CASE = "case_regression"


@dataclass(frozen=True, slots=True)
class RegressionFinding:
    kind: RegressionKind
    name: str  # criterion / case id (or "overall"/"pass_rate")
    baseline: float
    candidate: float
    drop: float

    def __str__(self) -> str:
        return (
            f"{self.kind.value}[{self.name}]: {self.baseline:.3f} -> "
            f"{self.candidate:.3f} (drop {self.drop:.3f})"
        )


@dataclass(frozen=True, slots=True)
class RegressionVerdict:
    """The result of comparing a candidate eval to a baseline."""

    regressed: bool
    findings: tuple[RegressionFinding, ...]
    overall_delta: float  # candidate - baseline (negative = worse)
    pass_rate_delta: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "regressed": self.regressed,
            "overall_delta": self.overall_delta,
            "pass_rate_delta": self.pass_rate_delta,
            "findings": [
                {
                    "kind": f.kind.value,
                    "name": f.name,
                    "baseline": f.baseline,
                    "candidate": f.candidate,
                    "drop": f.drop,
                }
                for f in self.findings
            ],
        }


@dataclass(frozen=True, slots=True)
class RegressionPolicy:
    """Pre-registered tolerances for what counts as a regression."""

    overall_tolerance: float = 0.03
    pass_rate_tolerance: float = 0.05
    criterion_tolerance: float = 0.10
    case_tolerance: float = 0.10


def detect(
    baseline: EvalReport,
    candidate: EvalReport,
    *,
    policy: RegressionPolicy | None = None,
) -> RegressionVerdict:
    """Compare a candidate report to a baseline and emit a verdict."""
    policy = policy or RegressionPolicy()
    findings: list[RegressionFinding] = []

    overall_delta = round(candidate.mean_score - baseline.mean_score, 6)
    if -overall_delta > policy.overall_tolerance:
        findings.append(
            RegressionFinding(
                RegressionKind.OVERALL,
                "overall",
                baseline.mean_score,
                candidate.mean_score,
                -overall_delta,
            )
        )

    pass_rate_delta = round(candidate.mean_pass_rate - baseline.mean_pass_rate, 6)
    if -pass_rate_delta > policy.pass_rate_tolerance:
        findings.append(
            RegressionFinding(
                RegressionKind.PASS_RATE,
                "pass_rate",
                baseline.mean_pass_rate,
                candidate.mean_pass_rate,
                -pass_rate_delta,
            )
        )

    for name, base_val in baseline.per_criterion_mean.items():
        cand_val = candidate.per_criterion_mean.get(name, 0.0)
        drop = base_val - cand_val
        if drop > policy.criterion_tolerance:
            findings.append(
                RegressionFinding(
                    RegressionKind.CRITERION, name, base_val, cand_val, round(drop, 6)
                )
            )

    for cid, base_val in baseline.per_case_mean.items():
        cand_val = candidate.per_case_mean.get(cid, 0.0)
        drop = base_val - cand_val
        if drop > policy.case_tolerance:
            findings.append(
                RegressionFinding(RegressionKind.CASE, cid, base_val, cand_val, round(drop, 6))
            )

    return RegressionVerdict(
        regressed=bool(findings),
        findings=tuple(findings),
        overall_delta=overall_delta,
        pass_rate_delta=pass_rate_delta,
    )


async def detect_from_harness(
    *,
    prompt_key: str,
    baseline_version: str,
    baseline_system: str,
    candidate_version: str,
    candidate_system: str,
    dataset: GoldenDataset,
    judge: Judge | None = None,
    responder: Responder | None = None,
    runs: int = 3,
    rubric: Rubric | None = None,
    policy: RegressionPolicy | None = None,
) -> tuple[RegressionVerdict, EvalReport, EvalReport]:
    """Run baseline + candidate evals and detect a regression in one call."""
    harness = EvalHarness(judge=judge) if judge is not None else EvalHarness()
    base_report = await harness.run(
        prompt_key=prompt_key,
        prompt_version=baseline_version,
        system=baseline_system,
        dataset=dataset,
        responder=responder,
        runs=runs,
        rubric=rubric,
    )
    cand_report = await harness.run(
        prompt_key=prompt_key,
        prompt_version=candidate_version,
        system=candidate_system,
        dataset=dataset,
        responder=responder,
        runs=runs,
        rubric=rubric,
    )
    verdict = detect(base_report, cand_report, policy=policy)
    return verdict, base_report, cand_report


__all__ = [
    "RegressionFinding",
    "RegressionKind",
    "RegressionPolicy",
    "RegressionVerdict",
    "detect",
    "detect_from_harness",
]
