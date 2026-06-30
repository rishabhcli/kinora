"""Shadow runner: the primary result is sacred, sampling gates, budget zero-guard."""

from __future__ import annotations

import pytest

from app.video.shadow.budget import EvalBudget
from app.video.shadow.clock import ManualClock
from app.video.shadow.collector import ComparisonDataset
from app.video.shadow.runner import ShadowRunner
from app.video.shadow.sampler import AlwaysSampler, DeterministicSampler
from app.video.shadow.seams import FailureKind, RenderOutcome, Sampler, ShotSpec

from .test_video_shadow_fakes import MapScorer, ScriptedProvider, make_outcome


def _spec(shot_id: str, duration_s: float = 5.0) -> ShotSpec:
    return ShotSpec(shot_id=shot_id, duration_s=duration_s)


def _funded_runner(
    candidate: ScriptedProvider,
    scorer: MapScorer,
    *,
    sampler: Sampler | None = None,
    cap: float = 1000.0,
    clock: ManualClock | None = None,
) -> tuple[ShadowRunner, EvalBudget]:
    budget = EvalBudget(cap_video_seconds=cap)
    runner = ShadowRunner(
        candidate=candidate,
        scorer=scorer,
        sampler=sampler or AlwaysSampler(),
        eval_budget=budget,
        clock=clock,
    )
    return runner, budget


async def test_primary_result_returned_unchanged() -> None:
    # The exact production outcome object comes back untouched.
    candidate = ScriptedProvider("cand", default=make_outcome("cand", video_seconds=5.0))
    scorer = MapScorer({"cand": 0.8, "prod": 0.6})
    runner, _ = _funded_runner(candidate, scorer)
    production = make_outcome("prod", video_seconds=5.0)
    obs = await runner.observe(_spec("s1"), production)
    assert obs.production is production
    # Production was *copied* for scoring inside the sample, not mutated in place.
    assert production.quality is None


async def test_not_sampled_skips_candidate_entirely() -> None:
    candidate = ScriptedProvider("cand")
    scorer = MapScorer({"cand": 0.8, "prod": 0.6})
    # Fraction 0 → nothing sampled.
    runner, budget = _funded_runner(candidate, scorer, sampler=DeterministicSampler(0.0))
    obs = await runner.observe(_spec("s1"), make_outcome("prod"))
    assert obs.sampled is False
    assert obs.sample is None
    assert candidate.rendered == []  # candidate never called
    assert budget.snapshot().committed_video_seconds == 0.0


async def test_zero_budget_never_renders_candidate() -> None:
    # The headline guarantee: shadow mode on, but unfunded → no candidate render,
    # no spend, recorded as GATED.
    candidate = ScriptedProvider("cand")
    scorer = MapScorer({"cand": 0.8, "prod": 0.6})
    budget = EvalBudget()  # zero by default
    runner = ShadowRunner(
        candidate=candidate, scorer=scorer, sampler=AlwaysSampler(), eval_budget=budget
    )
    obs = await runner.observe(_spec("s1"), make_outcome("prod"))
    assert obs.sampled is True
    assert candidate.rendered == []  # provider never invoked
    assert obs.sample is not None
    assert obs.sample.candidate.failure is FailureKind.GATED
    assert obs.sample.candidate.video_seconds == 0.0
    assert budget.snapshot().committed_video_seconds == 0.0


async def test_candidate_exception_isolated_from_primary() -> None:
    # A candidate provider that raises must not break observe(); the primary is
    # returned and the candidate is recorded as a provider error.
    candidate = ScriptedProvider("cand", raise_on=frozenset({"s1"}))
    scorer = MapScorer({"cand": 0.8, "prod": 0.6})
    runner, budget = _funded_runner(candidate, scorer)
    production = make_outcome("prod")
    obs = await runner.observe(_spec("s1"), production)
    assert obs.production is production
    assert obs.sample is not None
    assert obs.sample.candidate.failure is FailureKind.PROVIDER_ERROR
    # The reservation was released — no phantom spend.
    assert budget.snapshot().committed_video_seconds == 0.0
    assert budget.snapshot().reserved_video_seconds == 0.0


async def test_scorer_exception_does_not_abort_pairing() -> None:
    candidate = ScriptedProvider("cand", default=make_outcome("cand"))
    scorer = MapScorer({"cand": 0.8, "prod": 0.6}, raise_for=frozenset({"cand"}))
    runner, _ = _funded_runner(candidate, scorer)
    obs = await runner.observe(_spec("s1"), make_outcome("prod"))
    assert obs.sample is not None
    # Candidate succeeded but is left unscored (scorer blew up); production scored.
    assert obs.sample.candidate.quality is None
    assert obs.sample.production.quality == pytest.approx(0.6)


async def test_successful_pair_scores_both_and_settles_budget() -> None:
    candidate = ScriptedProvider("cand", default=make_outcome("cand", video_seconds=4.0))
    scorer = MapScorer({"cand": 0.85, "prod": 0.6})
    runner, budget = _funded_runner(candidate, scorer)
    obs = await runner.observe(_spec("s1", duration_s=4.0), make_outcome("prod"))
    sample = obs.sample
    assert sample is not None
    assert sample.candidate.quality == pytest.approx(0.85)
    assert sample.production.quality == pytest.approx(0.6)
    assert sample.quality_delta == pytest.approx(0.25)
    # Measured candidate spend committed against the eval budget.
    assert budget.snapshot().committed_video_seconds == pytest.approx(4.0)


async def test_latency_measured_from_clock_when_provider_omits_it() -> None:
    # Provider returns latency 0; the runner stamps elapsed monotonic time.
    clock = ManualClock()

    class TimingProvider(ScriptedProvider):
        async def render(self, spec: ShotSpec) -> RenderOutcome:
            clock.advance(2.5)  # 2.5s elapses during the render
            return make_outcome("cand", latency_ms=0.0, video_seconds=5.0)

    candidate = TimingProvider("cand")
    scorer = MapScorer({"cand": 0.7, "prod": 0.6})
    runner, _ = _funded_runner(candidate, scorer, clock=clock)
    obs = await runner.observe(_spec("s1"), make_outcome("prod"))
    assert obs.sample is not None
    assert obs.sample.candidate.latency_ms == pytest.approx(2500.0)


async def test_provider_supplied_latency_preserved() -> None:
    candidate = ScriptedProvider("cand", default=make_outcome("cand", latency_ms=1234.0))
    scorer = MapScorer({"cand": 0.7, "prod": 0.6})
    runner, _ = _funded_runner(candidate, scorer)
    obs = await runner.observe(_spec("s1"), make_outcome("prod"))
    assert obs.sample is not None
    assert obs.sample.candidate.latency_ms == pytest.approx(1234.0)


async def test_observe_into_appends_to_dataset() -> None:
    candidate = ScriptedProvider("cand", default=make_outcome("cand"))
    scorer = MapScorer({"cand": 0.8, "prod": 0.6})
    runner, _ = _funded_runner(candidate, scorer)
    ds = ComparisonDataset(candidate_model="cand", production_model="prod")
    for i in range(5):
        await runner.observe_into(_spec(f"s{i}"), make_outcome("prod"), ds)
    assert len(ds) == 5
    assert len(ds.comparable()) == 5


async def test_observe_into_skips_dataset_when_not_sampled() -> None:
    candidate = ScriptedProvider("cand")
    scorer = MapScorer({"cand": 0.8, "prod": 0.6})
    runner, _ = _funded_runner(candidate, scorer, sampler=DeterministicSampler(0.0))
    ds = ComparisonDataset(candidate_model="cand", production_model="prod")
    await runner.observe_into(_spec("s0"), make_outcome("prod"), ds)
    assert len(ds) == 0


async def test_sampling_only_renders_the_sampled_fraction() -> None:
    candidate = ScriptedProvider("cand", default=make_outcome("cand"))
    scorer = MapScorer({"cand": 0.8, "prod": 0.6})
    sampler = DeterministicSampler(0.5)
    # Fund the eval budget generously so this test isolates *sampling* from the
    # budget guard (each render bills 5s; ~1000 sampled shots ⇒ need > 5000s).
    runner, _ = _funded_runner(candidate, scorer, sampler=sampler, cap=1_000_000.0)
    ds = ComparisonDataset(candidate_model="cand", production_model="prod")
    n = 2000
    for i in range(n):
        await runner.observe_into(_spec(f"shot-{i}"), make_outcome("prod"), ds)
    # The number of candidate renders matches the sampled count, ~50%.
    expected = sum(sampler.in_sample(f"shot-{i}") for i in range(n))
    assert len(candidate.rendered) == expected
    assert len(ds) == expected
    assert abs(expected / n - 0.5) < 0.05


async def test_budget_exhaustion_midstream_gates_remaining_candidates() -> None:
    # A funded-but-small budget renders until it runs dry, then GATES the rest —
    # never overspends, and still records every sampled shot.
    candidate = ScriptedProvider("cand", default=make_outcome("cand", video_seconds=5.0))
    scorer = MapScorer({"cand": 0.8, "prod": 0.6})
    # 15s funds exactly 3 renders of 5s each; the 4th+ are gated.
    runner, budget = _funded_runner(candidate, scorer, cap=15.0)
    ds = ComparisonDataset(candidate_model="cand", production_model="prod")
    for i in range(10):
        await runner.observe_into(_spec(f"s{i}", duration_s=5.0), make_outcome("prod"), ds)
    assert len(candidate.rendered) == 3  # only 3 actually rendered
    assert budget.snapshot().committed_video_seconds == pytest.approx(15.0)
    assert budget.remaining() == 0.0
    gated = [s for s in ds.samples if s.candidate.failure is FailureKind.GATED]
    assert len(gated) == 7  # the rest recorded as gated, not rendered
