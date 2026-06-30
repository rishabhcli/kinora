"""Deterministic tests for the provider quality scoring harness (app/video/quality).

No infra, no network, no model: every perception axis is driven by a fake
:class:`VlScorer` and the frame stats are computed on tiny hand-built grids (or a
static feature extractor), so the math is fully reproducible. Covers each sub-score,
the weighted aggregation + flag-capping, the reputation ledger's EWMA decay / update /
confidence-shrink, and the cross-provider leaderboard.
"""

from __future__ import annotations

import math

import pytest

from app.video.quality import (
    FLAGGED_AGGREGATE_CAP,
    BenchmarkPrompt,
    BenchmarkRunner,
    BenchmarkSuite,
    ClipEvaluator,
    ClipSample,
    FrameFeatures,
    FrameStatsExtractor,
    ProviderSubmission,
    QualityLedger,
    QualityScore,
    QualityWeights,
    StaticFeatureExtractor,
    StaticVlScorer,
    SubmittedClip,
    SubScores,
    VlVerdict,
    alpha_from_half_life,
    banding_score,
    blockiness_score,
    blur_score,
    merge_into_ledger,
    motion_amount_score,
    temporal_flicker_score,
    weighted_aggregate,
)

# --------------------------------------------------------------------------- #
# tiny grid helpers
# --------------------------------------------------------------------------- #
Gray = list[list[float]]


def flat(value: float, n: int = 16) -> Gray:
    """An n×n constant-luminance frame."""
    return [[value] * n for _ in range(n)]


def gradient(n: int = 16) -> Gray:
    """A smooth left→right luminance ramp (many distinct levels, gentle gradients)."""
    return [[x / (n - 1) for x in range(n)] for _ in range(n)]


def checker(n: int = 16) -> Gray:
    """A high-frequency checkerboard (lots of edge energy → sharp)."""
    return [[float((x + y) % 2) for x in range(n)] for y in range(n)]


def one_hot(axis: int, dim: int = 8) -> list[float]:
    v = [0.0] * dim
    v[axis % dim] = 1.0
    return v


# --------------------------------------------------------------------------- #
# sub-score math: technical-integrity defect proxies
# --------------------------------------------------------------------------- #
def test_blur_score_blurry_vs_sharp() -> None:
    assert blur_score(flat(0.5)) == pytest.approx(1.0)  # no detail → fully blurry
    assert blur_score(checker()) == pytest.approx(0.0)  # max detail → sharp


def test_banding_score_flat_vs_smooth() -> None:
    assert banding_score(flat(0.5)) == pytest.approx(1.0)  # one level → full banding
    assert banding_score(gradient()) == pytest.approx(0.0)  # many levels → clean


def test_temporal_flicker_catches_luminance_strobe() -> None:
    steady = [flat(0.5), flat(0.5), flat(0.5)]
    assert temporal_flicker_score(steady) == pytest.approx(0.0)
    strobe = [flat(0.1), flat(0.9), flat(0.1)]  # 0.8 jump >> _FLICKER_FULL
    assert temporal_flicker_score(strobe) == pytest.approx(1.0)


def test_blockiness_prefers_macroblock_lattice() -> None:
    # Steps only on the 8-px column lattice → on-grid energy >> off-grid.
    n = 16
    frame = [[0.0 if (x // 8) % 2 == 0 else 1.0 for x in range(n)] for _ in range(n)]
    assert blockiness_score(frame, block=8) > 0.5
    # A frame with no lattice preference (uniform ramp) is not blocky.
    assert blockiness_score(gradient(), block=8) == pytest.approx(0.0, abs=0.05)


def test_motion_amount_static_vs_churning() -> None:
    assert motion_amount_score([flat(0.5), flat(0.5)]) == pytest.approx(0.0)
    assert motion_amount_score([flat(0.0), flat(1.0)]) == pytest.approx(1.0)


def test_frame_stats_extractor_clean_clip_high_integrity() -> None:
    ex = FrameStatsExtractor()
    feats = ex.extract([gradient(), gradient(), gradient()], [])
    assert feats.n_frames == 3
    assert feats.technical_integrity() > 0.85  # smooth + steady → near-pristine
    assert feats.temporal_flicker == pytest.approx(0.0)


def test_frame_stats_extractor_empty_is_zero() -> None:
    feats = FrameStatsExtractor().extract([], [])
    assert feats.n_frames == 0
    assert feats.technical_integrity() == pytest.approx(1.0)  # no defects observed


# --------------------------------------------------------------------------- #
# aggregation + flag-capping
# --------------------------------------------------------------------------- #
def test_weighted_aggregate_matches_manual_mean() -> None:
    sub = SubScores(
        technical_integrity=0.8,
        aesthetic=0.6,
        prompt_adherence=0.7,
        identity_consistency=0.9,
        style_consistency=0.85,
        motion_naturalness=0.5,
    )
    w = QualityWeights()
    expected = sum(
        w.as_mapping()[k] * sub.as_mapping()[k] for k in sub.as_mapping()
    ) / sum(w.as_mapping().values())
    assert weighted_aggregate(sub, w) == pytest.approx(round(expected, 6))


def test_zero_weights_fall_back_to_unweighted_mean() -> None:
    sub = SubScores(
        technical_integrity=0.2,
        aesthetic=0.4,
        prompt_adherence=0.6,
        identity_consistency=0.8,
        style_consistency=1.0,
        motion_naturalness=0.0,
    )
    zero = QualityWeights(
        technical_integrity=0.0,
        aesthetic=0.0,
        prompt_adherence=0.0,
        identity_consistency=0.0,
        style_consistency=0.0,
        motion_naturalness=0.0,
    )
    plain = sum(sub.as_mapping().values()) / 6
    assert weighted_aggregate(sub, zero) == pytest.approx(round(plain, 6))


def test_flag_caps_aggregate_not_zeroes_it() -> None:
    great = SubScores(
        technical_integrity=1.0,
        aesthetic=1.0,
        prompt_adherence=1.0,
        identity_consistency=1.0,
        style_consistency=1.0,
        motion_naturalness=1.0,
    )
    clean = QualityScore.from_subscores(great, provider="p")
    assert clean.aggregate == pytest.approx(1.0)
    assert not clean.flagged

    flagged = QualityScore.from_subscores(great, provider="p", nsfw_flag=True)
    assert flagged.flagged
    assert flagged.aggregate == pytest.approx(FLAGGED_AGGREGATE_CAP)
    # capped, not zeroed:
    assert flagged.aggregate > 0.0


# --------------------------------------------------------------------------- #
# ClipEvaluator: fuse features + VL + §9.5 consistency
# --------------------------------------------------------------------------- #
async def test_evaluator_neutral_consistency_without_refs() -> None:
    ev = ClipEvaluator(
        vl_scorer=StaticVlScorer(VlVerdict(aesthetic=0.7, prompt_adherence=0.8))
    )
    sample = ClipSample(
        clip_id="c1", provider="wan", prompt="a knight", gray=[gradient(), gradient()]
    )
    score = await ev.evaluate(sample)
    assert score.provider == "wan"
    assert score.sub_scores.identity_consistency == pytest.approx(1.0)  # no refs
    assert score.sub_scores.style_consistency == pytest.approx(1.0)  # no centroid
    assert score.sub_scores.aesthetic == pytest.approx(0.7)
    assert score.sub_scores.prompt_adherence == pytest.approx(0.8)
    assert not score.flagged


async def test_evaluator_identity_ccs_from_embeddings() -> None:
    ev = ClipEvaluator()
    aligned = ClipSample(
        clip_id="ok",
        gray=[gradient()],
        clip_embedding=one_hot(0),
        locked_refs=[one_hot(0)],
    )
    drifted = ClipSample(
        clip_id="bad",
        gray=[gradient()],
        clip_embedding=one_hot(1),
        locked_refs=[one_hot(0)],
    )
    s_ok = await ev.evaluate(aligned)
    s_bad = await ev.evaluate(drifted)
    # cos=1 → (1+1)/2 = 1.0 ; cos=0 (orthogonal) → 0.5
    assert s_ok.sub_scores.identity_consistency == pytest.approx(1.0)
    assert s_bad.sub_scores.identity_consistency == pytest.approx(0.5)
    assert s_ok.aggregate > s_bad.aggregate


async def test_evaluator_style_drift_lowers_style_axis() -> None:
    ev = ClipEvaluator()
    on_style = ClipSample(
        clip_id="s", gray=[gradient()], clip_style=one_hot(2), style_centroid=one_hot(2)
    )
    off_style = ClipSample(
        clip_id="d", gray=[gradient()], clip_style=one_hot(3), style_centroid=one_hot(2)
    )
    assert (await ev.evaluate(on_style)).sub_scores.style_consistency == pytest.approx(1.0)
    # orthogonal style: drift = 1 - 0 = 1 → consistency 0
    assert (await ev.evaluate(off_style)).sub_scores.style_consistency == pytest.approx(0.0)


async def test_evaluator_artifact_flag_on_catastrophic_frames() -> None:
    # Inject features that are nearly all defect → integrity below the floor.
    broken = FrameFeatures(
        n_frames=4, blockiness=1.0, blur=1.0, banding=1.0, temporal_flicker=1.0
    )
    ev = ClipEvaluator(extractor=StaticFeatureExtractor(broken))
    score = await ev.evaluate(ClipSample(clip_id="x", gray=[flat(0.5)]))
    assert score.artifact_flag
    assert score.aggregate <= FLAGGED_AGGREGATE_CAP


async def test_evaluator_nsfw_flag_from_vl() -> None:
    ev = ClipEvaluator(vl_scorer=StaticVlScorer(VlVerdict(nsfw_flag=True)))
    score = await ev.evaluate(ClipSample(clip_id="n", gray=[gradient()]))
    assert score.nsfw_flag and score.flagged
    assert score.aggregate <= FLAGGED_AGGREGATE_CAP


async def test_evaluator_motion_naturalness_tent() -> None:
    ev = ClipEvaluator()
    frozen = ClipEvaluator(
        extractor=StaticFeatureExtractor(FrameFeatures(n_frames=4, motion_amount=0.0))
    )
    chaotic = ClipEvaluator(
        extractor=StaticFeatureExtractor(FrameFeatures(n_frames=4, motion_amount=1.0))
    )
    natural = ClipEvaluator(
        extractor=StaticFeatureExtractor(FrameFeatures(n_frames=4, motion_amount=0.4))
    )
    s_frozen = await frozen.evaluate(ClipSample(clip_id="f"))
    s_chaotic = await chaotic.evaluate(ClipSample(clip_id="c"))
    s_natural = await natural.evaluate(ClipSample(clip_id="n"))
    assert s_natural.sub_scores.motion_naturalness == pytest.approx(1.0)
    assert s_frozen.sub_scores.motion_naturalness == pytest.approx(0.0)
    assert s_chaotic.sub_scores.motion_naturalness == pytest.approx(0.0)
    _ = ev  # default evaluator is exercised elsewhere


# --------------------------------------------------------------------------- #
# QualityLedger: EWMA decay / update / confidence-shrink
# --------------------------------------------------------------------------- #
def _score(provider: str, agg: float, flagged: bool = False) -> QualityScore:
    sub = SubScores(
        technical_integrity=agg,
        aesthetic=agg,
        prompt_adherence=agg,
        identity_consistency=agg,
        style_consistency=agg,
        motion_naturalness=agg,
    )
    return QualityScore.from_subscores(
        sub, provider=provider, nsfw_flag=flagged, weights=QualityWeights()
    )


def test_alpha_from_half_life() -> None:
    assert alpha_from_half_life(0) == pytest.approx(1.0)
    # after `h` updates an observation's weight halves: (1-alpha)^h == 0.5
    h = 4.0
    a = alpha_from_half_life(h)
    assert (1 - a) ** h == pytest.approx(0.5)


def test_ledger_seeds_to_first_aggregate() -> None:
    led = QualityLedger(half_life=10)
    rep = led.record(_score("wan", 0.8))
    assert rep.samples == 1
    # all-equal axes → the weighted aggregate equals that value; the first
    # observation seeds the EWMA exactly (no prior blending).
    assert led.snapshot("wan").score_ewma == pytest.approx(0.8)


def test_ledger_ewma_decays_toward_recent() -> None:
    led = QualityLedger(half_life=1.0)  # alpha = 0.5
    led.record(_score("wan", 1.0))  # seed = 1.0
    led.record(_score("wan", 0.0))  # 0.5*1.0 + 0.5*0.0 = 0.5
    snap = led.snapshot("wan")
    assert snap.samples == 2
    assert snap.score_ewma == pytest.approx(0.5)
    led.record(_score("wan", 0.0))  # 0.5*0.5 + 0.5*0 = 0.25
    assert led.snapshot("wan").score_ewma == pytest.approx(0.25)


def test_ledger_flag_rate_ewma_and_reputation_discount() -> None:
    led = QualityLedger(half_life=1.0)
    # Enough clean samples to clear the confidence floor, all flagged at the end.
    for _ in range(6):
        led.record(_score("bad", 0.9, flagged=True))
    snap = led.snapshot("bad")
    assert snap.flag_rate_ewma == pytest.approx(1.0)  # always flagged → rate 1
    # flagged aggregate is capped low AND reputation is flag-discounted to ~0.
    assert snap.reputation() == pytest.approx(0.0, abs=1e-6)


def test_ledger_confidence_shrink_under_sampled() -> None:
    led = QualityLedger(half_life=50)
    led.record(_score("lucky", 1.0))  # one perfect clip
    snap = led.snapshot("lucky")
    # 1 sample of 5-floor → confidence 0.2: 0.2*1.0 + 0.8*0.5 = 0.6, not 1.0.
    assert snap.reputation() == pytest.approx(0.6)
    assert snap.reputation() < 1.0


def test_ledger_clock_stamps_last_updated() -> None:
    ticks = iter([10.0, 20.0])
    led = QualityLedger(half_life=5, clock=lambda: next(ticks))
    led.record(_score("p", 0.5))
    assert led.snapshot("p").last_updated == 10.0
    led.record(_score("p", 0.5))
    assert led.snapshot("p").last_updated == 20.0


def test_ledger_ranked_and_best() -> None:
    led = QualityLedger(half_life=50)
    for _ in range(6):
        led.record(_score("good", 0.9))
        led.record(_score("mid", 0.6))
        led.record(_score("poor", 0.3))
    ranked = led.ranked()
    assert [r.provider for r in ranked] == ["good", "mid", "poor"]
    best = led.best()
    assert best is not None and best.provider == "good"


def test_ledger_empty_best_is_none() -> None:
    assert QualityLedger().best() is None


# --------------------------------------------------------------------------- #
# Benchmark runner + leaderboard
# --------------------------------------------------------------------------- #
def _suite() -> BenchmarkSuite:
    return BenchmarkSuite(
        name="canon-v1",
        prompts=[
            BenchmarkPrompt(
                prompt_id="knight",
                prompt="a knight on a hill",
                locked_refs=[one_hot(0)],
                style_centroid=one_hot(2),
            ),
            BenchmarkPrompt(
                prompt_id="forest",
                prompt="a misty forest",
                locked_refs=[one_hot(1)],
                style_centroid=one_hot(2),
            ),
        ],
    )


def _submission(provider: str, *, good: bool) -> ProviderSubmission:
    # "good" provider: clean frames + on-canon embeddings; "bad": defect-laden + drift.
    gray_good = [gradient(), gradient(), gradient()]
    gray_bad = [flat(0.1), flat(0.9), flat(0.1)]  # flicker + blur + banding
    return ProviderSubmission(
        provider=provider,
        clips={
            "knight": SubmittedClip(
                prompt_id="knight",
                gray=gray_good if good else gray_bad,
                clip_embedding=one_hot(0) if good else one_hot(5),
                clip_style=one_hot(2) if good else one_hot(7),
            ),
            "forest": SubmittedClip(
                prompt_id="forest",
                gray=gray_good if good else gray_bad,
                clip_embedding=one_hot(1) if good else one_hot(5),
                clip_style=one_hot(2) if good else one_hot(7),
            ),
        },
    )


async def test_benchmark_ranks_good_above_bad() -> None:
    runner = BenchmarkRunner(
        evaluator=ClipEvaluator(
            vl_scorer=StaticVlScorer(VlVerdict(aesthetic=0.7, prompt_adherence=0.8))
        )
    )
    suite = _suite()
    board = await runner.run(
        suite, [_submission("bad-model", good=False), _submission("good-model", good=True)]
    )
    assert board.winner == "good-model"
    assert [r.provider for r in board.results] == ["good-model", "bad-model"]
    good, bad = board.results[0], board.results[1]
    assert good.mean_aggregate > bad.mean_aggregate
    # good provider's identity axis stays high; bad drifts.
    assert good.axis_means()["identity_consistency"] > bad.axis_means()["identity_consistency"]


async def test_benchmark_report_dict_and_markdown() -> None:
    runner = BenchmarkRunner()
    board = await runner.run(_suite(), [_submission("only", good=True)])
    report = board.to_dict()
    assert report["suite"] == "canon-v1"
    assert report["winner"] == "only"
    providers = report["providers"]
    assert isinstance(providers, list) and providers[0]["rank"] == 1
    assert "axis_means" in providers[0]
    md = board.to_markdown()
    assert "leaderboard" in md.lower()
    assert "only" in md
    assert md.count("|") > 10  # a real table


async def test_benchmark_missing_clip_is_skipped() -> None:
    runner = BenchmarkRunner()
    suite = _suite()
    partial = ProviderSubmission(
        provider="partial",
        clips={"knight": SubmittedClip(prompt_id="knight", gray=[gradient()])},
    )
    result = await runner.run_provider(suite, partial)
    assert len(result.scores) == 1  # forest was missing → skipped
    assert result.reputation.samples == 1


async def test_merge_into_ledger_feeds_router_reputation() -> None:
    runner = BenchmarkRunner()
    board = await runner.run(
        _suite(), [_submission("a", good=True), _submission("b", good=False)]
    )
    router_ledger = QualityLedger(half_life=20)
    merge_into_ledger(
        router_ledger, {r.provider: r.scores for r in board.results}
    )
    reps = router_ledger.reputations()
    assert set(reps) == {"a", "b"}
    assert reps["a"].samples == 2 and reps["b"].samples == 2
    assert reps["a"].reputation() > reps["b"].reputation()


def test_merge_into_ledger_rejects_mismatch() -> None:
    led = QualityLedger()
    with pytest.raises(AssertionError):
        merge_into_ledger(led, {"a": [_score("b", 0.5)]})


def test_subscores_reject_out_of_range() -> None:
    with pytest.raises(ValueError):
        SubScores(technical_integrity=1.5)
    with pytest.raises(ValueError):
        QualityWeights(aesthetic=math.inf)
