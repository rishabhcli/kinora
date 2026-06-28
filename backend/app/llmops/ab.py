"""A/B experiments — two prompt versions over the *same* golden dataset.

§13's protocol is "fix everything; the only difference is the thing under test."
The A/B runner applies that to *prompts*: run version A and version B over the
identical dataset, with the identical responder + judge + runs, then compare them
**case-paired** (the same case scored under both arms) so the comparison controls
for case difficulty.

It reports:

* each arm's :class:`~app.llmops.harness.EvalReport`;
* the mean score delta (B − A) and the per-case paired deltas;
* a simple, dependency-free **effect size** (Cohen's *d* over the paired
  differences) and a paired-sign win/loss/tie count — enough to say "B is better,
  and the gap isn't a single lucky case" without pulling in scipy;
* a ``winner`` decision with a configurable minimum meaningful delta.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any

from app.llmops.datasets import GoldenDataset
from app.llmops.harness import EvalHarness, EvalReport, Responder
from app.llmops.judge import HeuristicJudge, Judge
from app.llmops.rubric import Rubric, get_rubric


@dataclass(frozen=True, slots=True)
class ABResult:
    """The paired comparison of two prompt versions."""

    dataset_name: str
    arm_a: EvalReport
    arm_b: EvalReport
    mean_delta: float  # B - A
    per_case_delta: dict[str, float]  # B - A per case
    cohens_d: float
    wins_b: int  # cases where B > A
    wins_a: int  # cases where A > B
    ties: int
    winner: str  # "B" | "A" | "tie"

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_name": self.dataset_name,
            "arm_a": self.arm_a.to_dict(),
            "arm_b": self.arm_b.to_dict(),
            "mean_delta": self.mean_delta,
            "per_case_delta": self.per_case_delta,
            "cohens_d": self.cohens_d,
            "wins_b": self.wins_b,
            "wins_a": self.wins_a,
            "ties": self.ties,
            "winner": self.winner,
        }


def _cohens_d(deltas: list[float]) -> float:
    """Cohen's d of the paired differences (mean / stdev). Zero when no spread."""
    if len(deltas) < 2:
        return 0.0
    sd = statistics.pstdev(deltas)
    if sd == 0.0:
        # All deltas identical: infinite-ish effect if non-zero, else zero.
        return 0.0 if deltas[0] == 0.0 else math.copysign(float("inf"), deltas[0])
    return round(statistics.fmean(deltas) / sd, 6)


@dataclass
class ABRunner:
    """Runs two prompt versions over one dataset and compares them, case-paired."""

    judge: Judge = None  # type: ignore[assignment]
    #: A |delta| below this is reported as a tie (avoid over-claiming on noise).
    min_meaningful_delta: float = 0.02

    def __post_init__(self) -> None:
        if self.judge is None:
            self.judge = HeuristicJudge()

    async def compare(
        self,
        *,
        prompt_key: str,
        version_a: str,
        system_a: str,
        version_b: str,
        system_b: str,
        dataset: GoldenDataset,
        responder_a: Responder | None = None,
        responder_b: Responder | None = None,
        runs: int = 3,
        rubric: Rubric | None = None,
    ) -> ABResult:
        rubric = rubric or get_rubric(dataset.rubric_name)
        harness = EvalHarness(judge=self.judge)
        arm_a = await harness.run(
            prompt_key=prompt_key,
            prompt_version=version_a,
            system=system_a,
            dataset=dataset,
            responder=responder_a,
            runs=runs,
            rubric=rubric,
        )
        arm_b = await harness.run(
            prompt_key=prompt_key,
            prompt_version=version_b,
            system=system_b,
            dataset=dataset,
            responder=responder_b,
            runs=runs,
            rubric=rubric,
        )

        per_case_delta = {
            cid: round(arm_b.per_case_mean.get(cid, 0.0) - score_a, 6)
            for cid, score_a in arm_a.per_case_mean.items()
        }
        deltas = list(per_case_delta.values())
        wins_b = sum(1 for d in deltas if d > 1e-9)
        wins_a = sum(1 for d in deltas if d < -1e-9)
        ties = sum(1 for d in deltas if abs(d) <= 1e-9)
        mean_delta = round(arm_b.mean_score - arm_a.mean_score, 6)

        if abs(mean_delta) < self.min_meaningful_delta:
            winner = "tie"
        else:
            winner = "B" if mean_delta > 0 else "A"

        return ABResult(
            dataset_name=dataset.name,
            arm_a=arm_a,
            arm_b=arm_b,
            mean_delta=mean_delta,
            per_case_delta=per_case_delta,
            cohens_d=_cohens_d(deltas),
            wins_b=wins_b,
            wins_a=wins_a,
            ties=ties,
            winner=winner,
        )


__all__ = ["ABResult", "ABRunner"]
