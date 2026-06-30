"""Replay mode: offline, deterministic, reuses recorded production outcomes."""

from __future__ import annotations

import pytest

from app.video.shadow.budget import EvalBudget
from app.video.shadow.replay import RecordedShot, ReplayCorpus, replay
from app.video.shadow.seams import FailureKind, ShotSpec

from .test_video_shadow_fakes import MapScorer, ScriptedProvider, make_outcome


def _corpus(n: int, *, with_recorded: bool) -> ReplayCorpus:
    shots: list[RecordedShot] = []
    for i in range(n):
        spec = ShotSpec(shot_id=f"s{i}", duration_s=5.0, prompt=f"p{i}")
        production = make_outcome("prod", quality=0.6, video_seconds=5.0) if with_recorded else None
        shots.append(RecordedShot(spec=spec, production=production))
    return ReplayCorpus(name="hist", production_model="prod", shots=shots)


async def test_replay_uses_recorded_production_no_production_provider() -> None:
    corpus = _corpus(10, with_recorded=True)
    candidate = ScriptedProvider("cand", default=make_outcome("cand", video_seconds=5.0))
    scorer = MapScorer({"cand": 0.8, "prod": 0.6})
    budget = EvalBudget(cap_video_seconds=1000.0)
    ds = await replay(corpus, candidate=candidate, scorer=scorer, eval_budget=budget)
    assert len(ds) == 10
    assert len(ds.comparable()) == 10
    assert ds.quality_deltas() == pytest.approx([0.2] * 10)
    # Candidate rendered every shot (AlwaysSampler under the hood).
    assert len(candidate.rendered) == 10


async def test_replay_renders_production_when_not_recorded() -> None:
    corpus = _corpus(5, with_recorded=False)
    candidate = ScriptedProvider("cand", default=make_outcome("cand"))
    production = ScriptedProvider("prod", default=make_outcome("prod"))
    scorer = MapScorer({"cand": 0.75, "prod": 0.6})
    budget = EvalBudget(cap_video_seconds=1000.0)
    ds = await replay(
        corpus, candidate=candidate, scorer=scorer, eval_budget=budget, production=production
    )
    assert len(ds) == 5
    assert production.rendered == [f"s{i}" for i in range(5)]


async def test_replay_raises_without_recorded_or_provider() -> None:
    corpus = _corpus(3, with_recorded=False)
    candidate = ScriptedProvider("cand", default=make_outcome("cand"))
    scorer = MapScorer({"cand": 0.7, "prod": 0.6})
    budget = EvalBudget(cap_video_seconds=1000.0)
    with pytest.raises(ValueError):
        await replay(corpus, candidate=candidate, scorer=scorer, eval_budget=budget)


async def test_replay_unfunded_budget_gates_candidate() -> None:
    corpus = _corpus(4, with_recorded=True)
    candidate = ScriptedProvider("cand", default=make_outcome("cand"))
    scorer = MapScorer({"cand": 0.8, "prod": 0.6})
    budget = EvalBudget()  # zero — replay must not spend
    ds = await replay(corpus, candidate=candidate, scorer=scorer, eval_budget=budget)
    assert len(ds) == 4
    assert candidate.rendered == []  # nothing rendered
    assert all(s.candidate.failure is FailureKind.GATED for s in ds.samples)
    assert budget.snapshot().committed_video_seconds == 0.0


async def test_replay_is_deterministic() -> None:
    corpus = _corpus(12, with_recorded=True)
    scorer = MapScorer({"cand": 0.72, "prod": 0.6})

    async def run() -> list[float]:
        candidate = ScriptedProvider("cand", default=make_outcome("cand"))
        budget = EvalBudget(cap_video_seconds=1000.0)
        ds = await replay(corpus, candidate=candidate, scorer=scorer, eval_budget=budget)
        return ds.quality_deltas()

    first = await run()
    second = await run()
    assert first == second


async def test_corpus_from_specs_helper() -> None:
    specs = [ShotSpec(shot_id=f"s{i}") for i in range(3)]
    recorded = {"s1": make_outcome("prod", quality=0.5)}
    corpus = ReplayCorpus.from_specs("prod", specs, productions=recorded)
    assert len(corpus) == 3
    assert corpus.shots[1].production is not None
    assert corpus.shots[0].production is None


async def test_corpus_round_trips_through_json() -> None:
    corpus = _corpus(3, with_recorded=True)
    restored = ReplayCorpus.model_validate_json(corpus.model_dump_json())
    assert len(restored) == 3
    assert restored.shots[0].spec.shot_id == "s0"
