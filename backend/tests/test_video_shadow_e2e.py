"""End-to-end: shadow over a simulated traffic stream → analysis → recommendation.

Exercises the whole harness as the orchestrator would: a stream of real renders,
a fractional sampler, a funded eval budget, then analysis + a promotion decision.
Deterministic throughout (scripted providers, fixed seed, no network).
"""

from __future__ import annotations

import pytest

from app.video.shadow.analysis import analyze
from app.video.shadow.budget import EvalBudget
from app.video.shadow.collector import ComparisonDataset
from app.video.shadow.recommendation import Verdict, recommend
from app.video.shadow.runner import ShadowRunner
from app.video.shadow.sampler import DeterministicSampler
from app.video.shadow.seams import ShotSpec

from .test_video_shadow_fakes import MapScorer, ScriptedProvider, make_outcome


async def test_full_flow_promotes_a_clearly_better_candidate() -> None:
    # Candidate is uniformly +0.12 quality, same cost, faster — a clear winner.
    candidate = ScriptedProvider(
        "cand-v2", default=make_outcome("cand-v2", video_seconds=5.0, latency_ms=900.0)
    )
    scorer = MapScorer({"cand-v2": 0.78, "prod-v1": 0.66})
    sampler = DeterministicSampler(0.5)
    budget = EvalBudget(cap_video_seconds=5_000.0)
    runner = ShadowRunner(candidate=candidate, scorer=scorer, sampler=sampler, eval_budget=budget)
    ds = ComparisonDataset(candidate_model="cand-v2", production_model="prod-v1")

    served: list[str] = []
    for i in range(500):
        production = make_outcome("prod-v1", video_seconds=5.0, latency_ms=1500.0)
        obs = await runner.observe_into(ShotSpec(shot_id=f"shot-{i}"), production, ds)
        # The reader always gets the production result, sampled or not.
        assert obs.production is production
        served.append(obs.production.clip_ref or "")

    # Roughly half were shadowed; the reader stream was never altered.
    assert all(ref == "prod-v1:ref" for ref in served)
    assert 0.4 < len(ds) / 500 < 0.6

    analysis = analyze(ds, bootstrap_seed=0)
    assert analysis.quality is not None
    assert analysis.quality.mean_delta == pytest.approx(0.12)
    assert analysis.latency.candidate_faster

    rec = recommend(analysis)
    assert rec.verdict is Verdict.PROMOTE

    # The reader budget was never charged here — only the *eval* budget moved.
    assert budget.snapshot().committed_video_seconds == pytest.approx(len(ds) * 5.0)


async def test_full_flow_rejects_a_worse_candidate() -> None:
    candidate = ScriptedProvider("cand-bad", default=make_outcome("cand-bad", video_seconds=5.0))
    scorer = MapScorer({"cand-bad": 0.5, "prod-v1": 0.7})
    budget = EvalBudget(cap_video_seconds=5_000.0)
    runner = ShadowRunner(
        candidate=candidate,
        scorer=scorer,
        sampler=DeterministicSampler(1.0),
        eval_budget=budget,
    )
    ds = ComparisonDataset(candidate_model="cand-bad", production_model="prod-v1")
    for i in range(60):
        await runner.observe_into(ShotSpec(shot_id=f"shot-{i}"), make_outcome("prod-v1"), ds)
    rec = recommend(analyze(ds))
    assert rec.verdict is Verdict.REJECT


async def test_full_flow_with_unfunded_budget_collects_only_gated() -> None:
    # Shadow enabled + sampled, but unfunded: a complete, non-spending dry run.
    candidate = ScriptedProvider("cand", default=make_outcome("cand"))
    scorer = MapScorer({"cand": 0.8, "prod": 0.6})
    budget = EvalBudget()  # zero by default
    runner = ShadowRunner(
        candidate=candidate, scorer=scorer, sampler=DeterministicSampler(1.0), eval_budget=budget
    )
    ds = ComparisonDataset(candidate_model="cand", production_model="prod")
    for i in range(40):
        await runner.observe_into(ShotSpec(shot_id=f"s{i}"), make_outcome("prod"), ds)
    # Every shot recorded, nothing rendered, nothing spent.
    assert len(ds) == 40
    assert candidate.rendered == []
    assert budget.snapshot().committed_video_seconds == 0.0
    # No comparable pairs (candidate all gated) → HOLD.
    rec = recommend(analyze(ds))
    assert rec.verdict is Verdict.HOLD
