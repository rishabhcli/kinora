"""Benchmark runner — score a fixed clip set per provider, emit a leaderboard + report.

To *pick* a provider you score every candidate on the **same** prompts and compare.
This module:

* defines a :class:`BenchmarkSuite` — a fixed, named set of prompts (the "ruler"),
  each pairing a prompt with optional locked refs / style centroid so identity + style
  consistency are measured against the same canon for every provider;
* runs a :class:`ProviderSubmission` (one provider's clips for the suite) through a
  :class:`~app.video.quality.evaluator.ClipEvaluator`, folding each score into a
  fresh per-provider :class:`~app.video.quality.ledger.QualityLedger`;
* aggregates across providers into a :class:`Leaderboard` (ranked reputations + per-
  axis means + flag rates) and emits a stable JSON-able dict + a human Markdown table
  for the comparison report.

Pure orchestration over the injected evaluator (which itself injects the feature + VL
seams), so a benchmark test runs entirely on fakes — no model, no decoding, no spend.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.core.logging import get_logger

from .evaluator import ClipEvaluator, ClipSample
from .ledger import ProviderReputation, QualityLedger
from .scores import QualityScore

logger = get_logger("app.video.quality.benchmark")

_AXES = (
    "technical_integrity",
    "aesthetic",
    "prompt_adherence",
    "identity_consistency",
    "style_consistency",
    "motion_naturalness",
)


@dataclass(frozen=True, slots=True)
class BenchmarkPrompt:
    """One fixed item in the suite — a prompt + the canon it is judged against."""

    prompt_id: str
    prompt: str
    locked_refs: list[list[float]] = field(default_factory=list)
    style_centroid: list[float] | None = None


@dataclass(frozen=True, slots=True)
class BenchmarkSuite:
    """A named, fixed set of prompts every provider is scored on (the shared ruler)."""

    name: str
    prompts: list[BenchmarkPrompt]

    @property
    def prompt_ids(self) -> list[str]:
        return [p.prompt_id for p in self.prompts]


@dataclass(frozen=True, slots=True)
class SubmittedClip:
    """A provider's rendered clip for one suite prompt (decoded frames + embeddings).

    The frame grids / embeddings mirror :class:`ClipSample`; the runner pairs this
    with the suite prompt's canon (locked refs / style centroid) to build the sample,
    so providers can't be judged against mismatched references.
    """

    prompt_id: str
    gray: list[list[list[float]]] = field(default_factory=list)
    rgb: list[list[list[tuple[float, float, float]]]] = field(default_factory=list)
    frames_raw: list[bytes] = field(default_factory=list)
    clip_embedding: list[float] | None = None
    clip_style: list[float] | None = None


@dataclass(frozen=True, slots=True)
class ProviderSubmission:
    """One provider's clips for the suite, keyed by prompt id."""

    provider: str
    clips: dict[str, SubmittedClip]


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """The benchmark outcome for one provider: per-clip scores + rolled reputation."""

    provider: str
    scores: list[QualityScore]
    reputation: ProviderReputation

    @property
    def mean_aggregate(self) -> float:
        if not self.scores:
            return 0.0
        return round(math.fsum(s.aggregate for s in self.scores) / len(self.scores), 6)

    def axis_means(self) -> dict[str, float]:
        if not self.scores:
            return dict.fromkeys(_AXES, 0.0)
        out: dict[str, float] = {}
        for axis in _AXES:
            vals = [s.sub_scores.as_mapping()[axis] for s in self.scores]
            out[axis] = round(math.fsum(vals) / len(vals), 6)
        return out

    def flag_rate(self) -> float:
        if not self.scores:
            return 0.0
        return round(sum(1 for s in self.scores if s.flagged) / len(self.scores), 6)


@dataclass(frozen=True, slots=True)
class Leaderboard:
    """The cross-provider comparison: ranked results + the winner."""

    suite: str
    results: list[ProviderResult]  # ranked best-first by reputation

    @property
    def winner(self) -> str | None:
        return self.results[0].provider if self.results else None

    def to_dict(self) -> dict[str, object]:
        """A stable JSON-able comparison report (for persistence / the operator UI)."""
        return {
            "suite": self.suite,
            "winner": self.winner,
            "providers": [
                {
                    "rank": i + 1,
                    "provider": r.provider,
                    "reputation": r.reputation.reputation(),
                    "score_ewma": r.reputation.score_ewma,
                    "mean_aggregate": r.mean_aggregate,
                    "flag_rate": r.flag_rate(),
                    "samples": r.reputation.samples,
                    "axis_means": r.axis_means(),
                }
                for i, r in enumerate(self.results)
            ],
        }

    def to_markdown(self) -> str:
        """A human comparison table (leaderboard) for the benchmark report."""
        header = (
            f"# Video quality leaderboard — suite `{self.suite}`\n\n"
            f"Winner: **{self.winner or 'n/a'}**\n\n"
            "| Rank | Provider | Reputation | Mean | Flag% | Tech | Aes | Prompt | "
            "Ident | Style | Motion |\n"
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
        )
        rows: list[str] = []
        for i, r in enumerate(self.results):
            ax = r.axis_means()
            rows.append(
                f"| {i + 1} | {r.provider} | {r.reputation.reputation():.3f} | "
                f"{r.mean_aggregate:.3f} | {r.flag_rate() * 100:.0f}% | "
                f"{ax['technical_integrity']:.2f} | {ax['aesthetic']:.2f} | "
                f"{ax['prompt_adherence']:.2f} | {ax['identity_consistency']:.2f} | "
                f"{ax['style_consistency']:.2f} | {ax['motion_naturalness']:.2f} |"
            )
        return header + "\n".join(rows) + "\n"


@dataclass(slots=True)
class BenchmarkRunner:
    """Runs providers through a suite and builds the leaderboard.

    Injects a :class:`ClipEvaluator` (default the fully-default one — pure features +
    neutral VL); pass an evaluator wired with a fake/real VL scorer to exercise the
    perception axes. ``ledger_half_life`` sizes each per-provider reputation EWMA.
    """

    evaluator: ClipEvaluator = field(default_factory=ClipEvaluator)
    ledger_half_life: float = 20.0

    def _sample_for(
        self, provider: str, prompt: BenchmarkPrompt, clip: SubmittedClip
    ) -> ClipSample:
        return ClipSample(
            clip_id=f"{provider}:{prompt.prompt_id}",
            provider=provider,
            prompt=prompt.prompt,
            gray=clip.gray,
            rgb=clip.rgb,
            frames_raw=clip.frames_raw,
            clip_embedding=clip.clip_embedding,
            locked_refs=prompt.locked_refs,
            clip_style=clip.clip_style,
            style_centroid=prompt.style_centroid,
        )

    async def run_provider(
        self, suite: BenchmarkSuite, submission: ProviderSubmission
    ) -> ProviderResult:
        """Score every suite prompt this provider submitted, in suite order."""
        ledger = QualityLedger(half_life=self.ledger_half_life)
        scores: list[QualityScore] = []
        for prompt in suite.prompts:
            clip = submission.clips.get(prompt.prompt_id)
            if clip is None:
                logger.warning(
                    "benchmark.missing_clip",
                    provider=submission.provider,
                    prompt_id=prompt.prompt_id,
                )
                continue
            sample = self._sample_for(submission.provider, prompt, clip)
            score = await self.evaluator.evaluate(sample)
            scores.append(score)
            ledger.record(score)
        reputation = (
            ledger.snapshot(submission.provider)
            if scores
            else ProviderReputation(provider=submission.provider, samples=0)
        )
        return ProviderResult(
            provider=submission.provider, scores=scores, reputation=reputation
        )

    async def run(
        self, suite: BenchmarkSuite, submissions: Sequence[ProviderSubmission]
    ) -> Leaderboard:
        """Score every provider on the suite and rank them into a leaderboard."""
        results = [await self.run_provider(suite, sub) for sub in submissions]
        results.sort(
            key=lambda r: (r.reputation.reputation(), r.reputation.samples, r.provider),
            reverse=True,
        )
        logger.info(
            "benchmark.complete",
            suite=suite.name,
            providers=len(results),
            winner=results[0].provider if results else None,
        )
        return Leaderboard(suite=suite.name, results=results)


def merge_into_ledger(
    ledger: QualityLedger, results: Mapping[str, Sequence[QualityScore]]
) -> None:
    """Fold benchmark scores into a long-lived router ledger (per-provider, in order).

    The benchmark builds throwaway per-provider ledgers for its own leaderboard; this
    helper feeds the *same* scores into a persistent, cross-run ledger that the router
    / registry consults, so a benchmark run updates live provider reputations.
    """
    for provider, scores in results.items():
        for score in scores:
            assert score.provider == provider, "score/provider mismatch in merge"
            ledger.record(score)
