"""The degradation-ladder planner (kinora.md §4.4/§12.4).

Pure, deterministic rung selection — no ffmpeg, DB, or network. Asserts the
planner reproduces ``pipeline._select_keyframe``'s priority order, that pressure
reasons forbid the live lane, that the fallback chain + cost classes are
monotone, and that the stats tally reports the ladder distribution.
"""

from __future__ import annotations

import pytest

from app.render.degrade import DegradeRung
from app.render.ladder import (
    DEGRADE_RUNGS,
    LADDER,
    LadderAssets,
    LadderReason,
    LadderStats,
    Rung,
    cost_class,
    degrade_chain,
    from_degrade_rung,
    plan_ladder,
    rank,
    to_degrade_rung,
)

# --------------------------------------------------------------------------- #
# Rung ordering + projection
# --------------------------------------------------------------------------- #


def test_ladder_is_richest_to_cheapest() -> None:
    assert LADDER[0] is Rung.FULL_WAN
    assert LADDER[-1] is Rung.AUDIO_TEXT_ONLY
    assert [rank(r) for r in LADDER] == [0, 1, 2, 3]
    # Cost class falls monotonically down the ladder.
    costs = [int(cost_class(r)) for r in LADDER]
    assert costs == sorted(costs, reverse=True)


def test_degrade_rung_projection_roundtrips() -> None:
    for rung in DEGRADE_RUNGS:
        assert from_degrade_rung(to_degrade_rung(rung)) is rung
    assert to_degrade_rung(Rung.KEN_BURNS_KEYFRAME) is DegradeRung.KEN_BURNS_KEYFRAME
    assert to_degrade_rung(Rung.AUDIO_TEXT_ONLY) is DegradeRung.AUDIO_TEXT_ONLY


def test_full_wan_has_no_degrade_rung() -> None:
    with pytest.raises(ValueError):
        to_degrade_rung(Rung.FULL_WAN)


# --------------------------------------------------------------------------- #
# Selection — live path
# --------------------------------------------------------------------------- #


def test_live_path_selected_when_feasible_and_reason_ok() -> None:
    assets = LadderAssets(live_feasible=True, has_keyframe=True, has_page_illustration=True)
    plan = plan_ladder(assets, LadderReason.LIVE_OK)
    assert plan.selected is Rung.FULL_WAN
    assert plan.is_live and not plan.is_degraded
    assert plan.degrade_rung is None
    # The chain spans every feasible lane below it, in order.
    assert plan.chain == (
        Rung.FULL_WAN,
        Rung.KEN_BURNS_KEYFRAME,
        Rung.KEN_BURNS_ILLUSTRATION,
        Rung.AUDIO_TEXT_ONLY,
    )
    assert plan.fallback is Rung.KEN_BURNS_KEYFRAME


@pytest.mark.parametrize(
    "reason",
    [
        LadderReason.LIVE_VIDEO_DISABLED,
        LadderReason.BUDGET_LOW,
        LadderReason.BUDGET_EXCEEDED,
        LadderReason.RETRIES_EXHAUSTED,
        LadderReason.PROVIDER_ERROR,
        LadderReason.POISONED,
    ],
)
def test_pressure_reason_forbids_live_lane_even_when_feasible(reason: LadderReason) -> None:
    assets = LadderAssets(live_feasible=True, has_keyframe=True)
    plan = plan_ladder(assets, reason)
    assert plan.is_degraded
    assert plan.selected is Rung.KEN_BURNS_KEYFRAME
    assert plan.lane(Rung.FULL_WAN).feasible is False


# --------------------------------------------------------------------------- #
# Selection — degradation rungs mirror pipeline._select_keyframe priority
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "still_flag",
    ["has_keyframe", "has_locked_ref", "has_prev_endpoint", "can_image_gen"],
)
def test_any_still_source_picks_the_keyframe_rung(still_flag: str) -> None:
    # Each of the four still sources independently lands the keyframe rung — the
    # same four-way fallback the pipeline's _select_keyframe walks.
    assets = LadderAssets(**{still_flag: True})
    plan = plan_ladder(assets, LadderReason.LIVE_VIDEO_DISABLED)
    assert plan.selected is Rung.KEN_BURNS_KEYFRAME
    assert plan.degrade_rung is DegradeRung.KEN_BURNS_KEYFRAME


def test_page_illustration_when_no_still() -> None:
    assets = LadderAssets(has_page_illustration=True)
    plan = plan_ladder(assets, LadderReason.BUDGET_LOW)
    assert plan.selected is Rung.KEN_BURNS_ILLUSTRATION
    assert plan.degrade_rung is DegradeRung.KEN_BURNS_ILLUSTRATION
    assert plan.chain == (Rung.KEN_BURNS_ILLUSTRATION, Rung.AUDIO_TEXT_ONLY)
    assert plan.fallback is Rung.AUDIO_TEXT_ONLY


def test_audio_card_is_the_floor_when_nothing_else() -> None:
    plan = plan_ladder(LadderAssets(), LadderReason.RETRIES_EXHAUSTED)
    assert plan.selected is Rung.AUDIO_TEXT_ONLY
    assert plan.degrade_rung is DegradeRung.AUDIO_TEXT_ONLY
    assert plan.chain == (Rung.AUDIO_TEXT_ONLY,)
    assert plan.fallback is None  # the film never hard-stops, but this is the bottom


def test_audio_card_feasible_even_without_narration_audio() -> None:
    # A TTS outage still ships a (silent) card — never a crash (§4.11).
    plan = plan_ladder(LadderAssets(has_narration_audio=False), LadderReason.PROVIDER_ERROR)
    assert plan.selected is Rung.AUDIO_TEXT_ONLY
    assert plan.lane(Rung.AUDIO_TEXT_ONLY).feasible is True


def test_keyframe_preferred_over_illustration_when_both_present() -> None:
    assets = LadderAssets(has_locked_ref=True, has_page_illustration=True)
    plan = plan_ladder(assets, LadderReason.LIVE_VIDEO_DISABLED)
    assert plan.selected is Rung.KEN_BURNS_KEYFRAME
    # Both lower lanes remain in the chain as fallbacks, in order.
    assert plan.chain == (
        Rung.KEN_BURNS_KEYFRAME,
        Rung.KEN_BURNS_ILLUSTRATION,
        Rung.AUDIO_TEXT_ONLY,
    )


# --------------------------------------------------------------------------- #
# Determinism + explainability
# --------------------------------------------------------------------------- #


def test_plan_is_deterministic() -> None:
    assets = LadderAssets(has_keyframe=True, has_page_illustration=True)
    a = plan_ladder(assets, LadderReason.BUDGET_LOW)
    b = plan_ladder(assets, LadderReason.BUDGET_LOW)
    assert a == b


def test_lane_feasibility_reports_missing_inputs() -> None:
    plan = plan_ladder(LadderAssets(), LadderReason.LIVE_VIDEO_DISABLED)
    assert "live_feasible" in plan.lane(Rung.FULL_WAN).missing
    assert "page_illustration" in plan.lane(Rung.KEN_BURNS_ILLUSTRATION).missing
    assert "keyframe" in plan.lane(Rung.KEN_BURNS_KEYFRAME).missing


def test_degrade_chain_forces_a_degrade_reason() -> None:
    # Passing LIVE_OK to degrade_chain still yields a degradation chain.
    chain = degrade_chain(LadderAssets(has_keyframe=True), LadderReason.LIVE_OK)
    assert Rung.FULL_WAN not in chain
    assert chain[0] is Rung.KEN_BURNS_KEYFRAME


# --------------------------------------------------------------------------- #
# Ladder distribution stats
# --------------------------------------------------------------------------- #


def test_ladder_stats_tally_and_fractions() -> None:
    stats = LadderStats()
    for _ in range(6):
        stats.record(Rung.FULL_WAN)
    for _ in range(3):
        stats.record(Rung.KEN_BURNS_KEYFRAME)
    stats.record(Rung.AUDIO_TEXT_ONLY)
    assert stats.total == 10
    assert stats.live_fraction == pytest.approx(0.6)
    assert stats.fraction(Rung.KEN_BURNS_KEYFRAME) == pytest.approx(0.3)
    assert stats.as_dict()[Rung.AUDIO_TEXT_ONLY.value] == 1


def test_ladder_stats_record_plan_and_merge() -> None:
    a = LadderStats()
    a.record_plan(plan_ladder(LadderAssets(live_feasible=True), LadderReason.LIVE_OK))
    b = LadderStats()
    b.record_plan(plan_ladder(LadderAssets(has_keyframe=True), LadderReason.BUDGET_LOW))
    merged = a.merge([b])
    assert merged.counts[Rung.FULL_WAN] == 1
    assert merged.counts[Rung.KEN_BURNS_KEYFRAME] == 1
    assert merged.total == 2
    # Merge must not mutate the originals.
    assert a.total == 1 and b.total == 1


def test_empty_stats_fractions_are_zero() -> None:
    stats = LadderStats()
    assert stats.total == 0
    assert stats.live_fraction == 0.0
    assert stats.fraction(Rung.FULL_WAN) == 0.0
