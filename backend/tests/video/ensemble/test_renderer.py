"""End-to-end best-of-N renderer tests with deterministic fakes: fan-out + select by
objective, disabled-by-default no-fanout, early-stop cancels losers, cost-cap abort,
consistency vote, ledger settlement (winner committed / losers released), and the
emitted selection report. No network, no real video, no KINORA_LIVE_VIDEO."""

from __future__ import annotations

import asyncio

import pytest

from app.video.ensemble.models import (
    CandidateStatus,
    CostUnit,
    EnsembleConfig,
    Objective,
    ProviderChoice,
    QualityScore,
)
from app.video.ensemble.renderer import BestOfNRenderer

from ._fakes import FakeBudget, FakeProvider, FakeScorer, spec


def _cfg(**kw: object) -> EnsembleConfig:
    base: dict[str, object] = {
        "enabled": True,
        "enabled_tiers": frozenset({"hero"}),
        "max_candidates": 3,
        "max_concurrency": 3,
    }
    base.update(kw)
    return EnsembleConfig(**base)


def _choices(*names: str, cost_per_s: float = 1.0) -> list[ProviderChoice]:
    return [ProviderChoice(name=n, cost_per_s=cost_per_s, priority=i) for i, n in enumerate(names)]


def _renderer(
    providers: dict[str, FakeProvider],
    scorer: FakeScorer,
    budget: FakeBudget,
    cfg: EnsembleConfig,
) -> BestOfNRenderer:
    return BestOfNRenderer(dict(providers), scorer, budget, cfg)


# --------------------------------------------------------------------------- #
# Best-by-objective through the full pipeline
# --------------------------------------------------------------------------- #


async def test_fanout_selects_best_quality_and_settles_ledger() -> None:
    providers = {n: FakeProvider(n) for n in ("a", "b", "c")}
    scorer = FakeScorer(
        {
            "a": QualityScore(composite=0.70),
            "b": QualityScore(composite=0.93),
            "c": QualityScore(composite=0.81),
        }
    )
    budget = FakeBudget()
    renderer = _renderer(providers, scorer, budget, _cfg(objective=Objective.MAX_QUALITY))

    report = await renderer.render(spec(), _choices("a", "b", "c"))

    assert report.fanned_out is True
    assert report.winner == "b"
    assert report.winning_score == pytest.approx(0.93)
    # All three rendered + scored.
    assert {c.provider for c in report.eligible} == {"a", "b", "c"}
    # Winner committed (5s), losers released — net charge = 5s only.
    assert budget.commits and budget.net_committed == pytest.approx(5.0)
    assert sorted(budget.releases) and len(budget.releases) == 2
    assert budget.outstanding == {}  # everything settled
    assert report.charged_video_seconds == pytest.approx(5.0)


async def test_quality_per_cost_objective_end_to_end() -> None:
    providers = {"a": FakeProvider("a"), "b": FakeProvider("b")}
    scorer = FakeScorer({"a": QualityScore(composite=0.96), "b": QualityScore(composite=0.90)})
    budget = FakeBudget()
    # a is pricier (cost_per_s 4) → worse value despite higher quality.
    choices = [
        ProviderChoice(name="a", cost_per_s=4.0, priority=0),
        ProviderChoice(name="b", cost_per_s=1.0, priority=1),
    ]
    renderer = _renderer(providers, scorer, budget, _cfg(objective=Objective.QUALITY_PER_COST))
    report = await renderer.render(spec(), choices)
    assert report.winner == "b"
    assert "value" in report.reason


async def test_consistency_vote_end_to_end() -> None:
    providers = {n: FakeProvider(n) for n in ("a", "b")}
    scorer = FakeScorer(
        {
            "a": QualityScore(composite=0.95, identity=0.80),
            "b": QualityScore(composite=0.85, identity=0.98),
        }
    )
    budget = FakeBudget()
    renderer = _renderer(providers, scorer, budget, _cfg(objective=Objective.CONSISTENCY_VOTE))
    report = await renderer.render(spec(), _choices("a", "b"))
    assert report.winner == "b"  # most on-model
    assert "on-model" in report.reason


# --------------------------------------------------------------------------- #
# Disabled-by-default: no fan-out guard
# --------------------------------------------------------------------------- #


async def test_disabled_by_default_renders_single_no_fanout() -> None:
    providers = {n: FakeProvider(n) for n in ("a", "b", "c")}
    scorer = FakeScorer(default=QualityScore(composite=0.5))
    budget = FakeBudget()
    # Bare default config: enabled=False, max_candidates=1.
    renderer = _renderer(providers, scorer, budget, EnsembleConfig())

    report = await renderer.render(spec(), _choices("a", "b", "c"))

    assert report.fanned_out is False
    assert report.winner == "a"  # only the best-priority provider ran
    assert providers["a"].calls == 1
    assert providers["b"].calls == 0 and providers["c"].calls == 0
    # The others appear in the report as skipped, for honesty.
    skipped = [c for c in report.candidates if c.status is CandidateStatus.SKIPPED]
    assert {c.provider for c in skipped} == {"b", "c"}
    assert budget.net_committed == pytest.approx(5.0)  # one render charged


async def test_tier_not_enabled_does_not_fanout() -> None:
    providers = {n: FakeProvider(n) for n in ("a", "b")}
    renderer = _renderer(providers, FakeScorer(), FakeBudget(), _cfg())
    report = await renderer.render(spec(tier="standard"), _choices("a", "b"))
    assert report.fanned_out is False
    assert providers["b"].calls == 0


async def test_live_gate_off_still_renders_single_but_no_fanout() -> None:
    providers = {n: FakeProvider(n) for n in ("a", "b")}
    budget = FakeBudget(live=False)
    renderer = _renderer(providers, FakeScorer(), budget, _cfg())
    report = await renderer.render(spec(), _choices("a", "b"))
    assert report.fanned_out is False
    assert providers["a"].calls == 1 and providers["b"].calls == 0


# --------------------------------------------------------------------------- #
# Early-stop cancels losers
# --------------------------------------------------------------------------- #


async def test_early_stop_cancels_in_flight_losers() -> None:
    # a is fast + good-enough; b/c block until released so the early-stop can cancel them.
    gate = asyncio.Event()
    providers = {
        "a": FakeProvider("a"),
        "b": FakeProvider("b", gate=gate),
        "c": FakeProvider("c", gate=gate),
    }
    scorer = FakeScorer(
        {
            "a": QualityScore(composite=0.95),
            "b": QualityScore(composite=0.99),
            "c": QualityScore(composite=0.99),
        }
    )
    budget = FakeBudget()
    cfg = _cfg(good_enough_quality=0.90, max_concurrency=3)
    renderer = _renderer(providers, scorer, budget, cfg)

    report = await renderer.render(spec(), _choices("a", "b", "c"))

    assert report.early_stopped is True
    assert report.winner == "a"  # the good-enough candidate wins, losers cancelled
    statuses = {c.provider: c.status for c in report.candidates}
    assert statuses["b"] is CandidateStatus.CANCELLED
    assert statuses["c"] is CandidateStatus.CANCELLED
    # Cancelled losers' reservations were released → only a's 5s is charged.
    assert budget.net_committed == pytest.approx(5.0)
    assert budget.outstanding == {}
    assert providers["b"].cancelled and providers["c"].cancelled


async def test_early_stop_suppresses_not_yet_launched_candidates() -> None:
    # max_concurrency=1 forces serial launch; the first is good-enough so the rest
    # are never launched at all.
    providers = {n: FakeProvider(n) for n in ("a", "b", "c")}
    scorer = FakeScorer(default=QualityScore(composite=0.97))
    budget = FakeBudget()
    cfg = _cfg(good_enough_quality=0.90, max_concurrency=1)
    renderer = _renderer(providers, scorer, budget, cfg)

    report = await renderer.render(spec(), _choices("a", "b", "c"))

    assert report.early_stopped is True
    assert report.winner == "a"
    assert providers["a"].calls == 1
    assert providers["b"].calls == 0 and providers["c"].calls == 0
    assert budget.net_committed == pytest.approx(5.0)
    assert budget.outstanding == {}


async def test_no_early_stop_when_none_good_enough() -> None:
    providers = {n: FakeProvider(n) for n in ("a", "b")}
    scorer = FakeScorer(default=QualityScore(composite=0.50))
    budget = FakeBudget()
    cfg = _cfg(good_enough_quality=0.90)
    renderer = _renderer(providers, scorer, budget, cfg)
    report = await renderer.render(spec(), _choices("a", "b"))
    assert report.early_stopped is False
    assert providers["a"].calls == 1 and providers["b"].calls == 1


# --------------------------------------------------------------------------- #
# Cost-cap respected + abort mid fan-out
# --------------------------------------------------------------------------- #


async def test_cost_cap_aborts_over_cap_candidates_and_picks_within() -> None:
    providers = {n: FakeProvider(n) for n in ("a", "b", "c")}
    scorer = FakeScorer(
        {
            "a": QualityScore(composite=0.80),
            "b": QualityScore(composite=0.90),
            "c": QualityScore(composite=0.99),
        }
    )
    budget = FakeBudget()
    # Each candidate is 5s; cap 12s so only the first two fit; the third aborts.
    cfg = _cfg(
        objective=Objective.QUALITY_UNDER_COST_CAP,
        per_shot_cost_cap=12.0,
        cost_unit=CostUnit.VIDEO_SECONDS,
        max_concurrency=1,  # serial so the cap accounting is deterministic
    )
    renderer = _renderer(providers, scorer, budget, cfg)

    report = await renderer.render(spec(duration_s=5.0), _choices("a", "b", "c"))

    # c never renders (its launch would breach 12s) → OVER_CAP, no provider call.
    over = {c.provider: c.status for c in report.candidates}
    assert over["c"] is CandidateStatus.OVER_CAP
    assert providers["c"].calls == 0
    # Of the within-cap pair, b has the higher composite → winner.
    assert report.winner == "b"
    # Only the winner is charged; the other within-cap loser is released.
    assert budget.net_committed == pytest.approx(5.0)
    assert budget.outstanding == {}


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #


async def test_failed_render_is_a_losing_candidate_not_a_crash() -> None:
    providers = {
        "a": FakeProvider("a", error=RuntimeError("provider exploded")),
        "b": FakeProvider("b"),
    }
    scorer = FakeScorer({"b": QualityScore(composite=0.7)})
    budget = FakeBudget()
    renderer = _renderer(providers, scorer, budget, _cfg())
    report = await renderer.render(spec(), _choices("a", "b"))
    assert report.winner == "b"
    statuses = {c.provider: c.status for c in report.candidates}
    assert statuses["a"] is CandidateStatus.FAILED
    # The failed candidate's reservation was released; only b charged.
    assert budget.net_committed == pytest.approx(5.0)
    assert budget.outstanding == {}


async def test_score_failure_is_handled() -> None:
    providers = {n: FakeProvider(n) for n in ("a", "b")}
    scorer = FakeScorer({"b": QualityScore(composite=0.8)}, error_for={"a"})
    budget = FakeBudget()
    renderer = _renderer(providers, scorer, budget, _cfg())
    report = await renderer.render(spec(), _choices("a", "b"))
    assert report.winner == "b"
    assert {c.provider: c.status for c in report.candidates}["a"] is CandidateStatus.SCORE_FAILED
    assert budget.outstanding == {}


async def test_all_candidates_fail_yields_no_winner() -> None:
    providers = {
        "a": FakeProvider("a", error=RuntimeError("x")),
        "b": FakeProvider("b", error=RuntimeError("y")),
    }
    budget = FakeBudget()
    renderer = _renderer(providers, FakeScorer(), budget, _cfg())
    report = await renderer.render(spec(), _choices("a", "b"))
    assert report.winner is None
    assert report.charged_video_seconds == pytest.approx(0.0)
    assert budget.net_committed == pytest.approx(0.0)
    assert budget.outstanding == {}


# --------------------------------------------------------------------------- #
# Determinism + report
# --------------------------------------------------------------------------- #


async def test_winner_is_deterministic_across_runs() -> None:
    async def run() -> str | None:
        providers = {n: FakeProvider(n) for n in ("a", "b", "c")}
        scorer = FakeScorer(default=QualityScore(composite=0.80))  # all tie
        renderer = _renderer(providers, scorer, FakeBudget(), _cfg())
        report = await renderer.render(spec(), _choices("a", "b", "c"))
        return report.winner

    winners = {await run() for _ in range(5)}
    assert winners == {"a"}  # earliest launch order, every time


async def test_selection_report_is_complete_and_log_safe() -> None:
    providers = {n: FakeProvider(n) for n in ("a", "b")}
    scorer = FakeScorer({"a": QualityScore(composite=0.6), "b": QualityScore(composite=0.9)})
    budget = FakeBudget()
    renderer = _renderer(providers, scorer, budget, _cfg(objective=Objective.MAX_QUALITY))
    report = await renderer.render(spec(shot_id="shot-42"), _choices("a", "b"))

    assert report.shot_id == "shot-42"
    assert report.objective is Objective.MAX_QUALITY
    assert report.enabled is True
    assert len(report.candidates) == 2
    # Every candidate carries its score + cost for audit.
    for c in report.candidates:
        assert c.score is not None and c.video_seconds == pytest.approx(5.0)
    fields = report.as_log_fields()
    assert fields["winner"] == "b"
    assert fields["winning_score"] == pytest.approx(0.9)
    assert fields["candidates"] == 2 and fields["eligible"] == 2
    assert "reason" in fields and fields["fanned_out"] is True


async def test_unknown_providers_dropped_from_fanout() -> None:
    providers = {"a": FakeProvider("a")}
    scorer = FakeScorer(default=QualityScore(composite=0.7))
    renderer = _renderer(providers, scorer, FakeBudget(), _cfg())
    # "ghost" has no backend → silently dropped; only "a" runs.
    report = await renderer.render(spec(), _choices("a", "ghost"))
    assert report.winner == "a"
    assert {c.provider for c in report.candidates} == {"a"}


async def test_max_candidates_caps_fanout_width() -> None:
    providers = {n: FakeProvider(n) for n in ("a", "b", "c", "d")}
    scorer = FakeScorer(default=QualityScore(composite=0.7))
    budget = FakeBudget()
    cfg = _cfg(max_candidates=2)
    renderer = _renderer(providers, scorer, budget, cfg)
    report = await renderer.render(spec(), _choices("a", "b", "c", "d"))
    # Only the top-2 priority providers actually render.
    assert providers["a"].calls == 1 and providers["b"].calls == 1
    assert providers["c"].calls == 0 and providers["d"].calls == 0
    tail = {c.provider: c.status for c in report.candidates}
    assert tail["c"] is CandidateStatus.SKIPPED and tail["d"] is CandidateStatus.SKIPPED
