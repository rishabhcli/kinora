"""Replay mode — re-run a recorded set of shot specs against a candidate, offline.

Shadow mode evaluates a candidate against *live* traffic. Replay evaluates it
against a *frozen* corpus: a recorded list of historical :class:`ShotSpec` (and,
optionally, the production outcomes they previously produced). This lets you:

* benchmark a brand-new candidate before exposing it to any live request,
* re-evaluate every past candidate against the same fixed corpus for apples-to-
  apples comparison, and
* reproduce a prior eval bit-for-bit (the corpus + the seeded analysis are the
  only inputs).

It reuses the exact same machinery as live shadow mode — the candidate provider,
the scorer, the eval budget, and the collector — so a candidate cannot behave
differently under replay than under shadow. The only differences are: the specs
come from a corpus instead of live traffic, and (by default) every spec is
processed (an :class:`AlwaysSampler`), since the corpus is already the sample.

Determinism: given the same corpus, the same (deterministic) fakes, and the same
budget, :func:`replay` produces an identical :class:`ComparisonDataset` every run —
there is no clock-derived ordering and no RNG in the replay loop itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .budget import EvalBudget
from .clock import MonotonicClock
from .collector import ComparisonDataset
from .runner import ShadowRunner
from .sampler import AlwaysSampler
from .seams import Clock, QualityScorer, RenderOutcome, ShotSpec, VideoRenderProvider


class RecordedShot(BaseModel):
    """One entry in a replay corpus: a spec plus its prior production outcome.

    If ``production`` is omitted, replay re-renders the production model from the
    same ``spec`` (the orchestrator supplies a production provider). Recording the
    prior production outcome instead lets a fully-offline replay avoid *any*
    production render — the historical result is reused as the paired reference.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec: ShotSpec
    production: RenderOutcome | None = None


class ReplayCorpus(BaseModel):
    """An ordered, serialisable set of recorded shots to replay against a candidate."""

    model_config = ConfigDict(extra="forbid")

    name: str = "replay"
    production_model: str
    shots: list[RecordedShot] = Field(default_factory=list)

    def __len__(self) -> int:
        return len(self.shots)

    @classmethod
    def from_specs(
        cls,
        production_model: str,
        specs: Sequence[ShotSpec],
        *,
        productions: Mapping[str, RenderOutcome] | None = None,
        name: str = "replay",
    ) -> ReplayCorpus:
        """Build a corpus from bare specs, optionally attaching recorded outcomes."""
        productions = productions or {}
        shots = [
            RecordedShot(spec=spec, production=productions.get(spec.shot_id)) for spec in specs
        ]
        return cls(name=name, production_model=production_model, shots=shots)


async def replay(
    corpus: ReplayCorpus,
    *,
    candidate: VideoRenderProvider,
    scorer: QualityScorer,
    eval_budget: EvalBudget,
    production: VideoRenderProvider | None = None,
    clock: Clock | None = None,
) -> ComparisonDataset:
    """Replay ``corpus`` against ``candidate``; return the paired dataset.

    For each recorded shot the production reference is, in order of preference:
    (1) the recorded outcome on the shot, else (2) a fresh render from the injected
    ``production`` provider. Exactly one must be available per shot, else a
    :class:`ValueError` is raised (a corpus with neither recorded outcomes nor a
    production provider can't be paired).

    Every shot is processed (``AlwaysSampler``); the candidate is metered against
    ``eval_budget`` exactly as in live shadow mode, so an unfunded budget yields a
    dataset of GATED candidate outcomes (still a valid, if uninformative, run).
    """
    runner = ShadowRunner(
        candidate=candidate,
        scorer=scorer,
        sampler=AlwaysSampler(),
        eval_budget=eval_budget,
        clock=clock or MonotonicClock(),
    )
    dataset = ComparisonDataset(
        candidate_model=candidate.model_id,
        production_model=corpus.production_model,
    )
    for shot in corpus.shots:
        production_outcome = await _production_for(shot, production)
        await runner.observe_into(shot.spec, production_outcome, dataset)
    return dataset


async def _production_for(
    shot: RecordedShot,
    production: VideoRenderProvider | None,
) -> RenderOutcome:
    if shot.production is not None:
        return shot.production
    if production is None:
        raise ValueError(
            f"shot {shot.spec.shot_id!r} has no recorded production outcome and no "
            "production provider was supplied to replay()"
        )
    return await production.render(shot.spec)


__all__ = ["RecordedShot", "ReplayCorpus", "replay"]
