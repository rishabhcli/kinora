"""Unit tests for series-scale showrunning (§7, §7.2).

Everything under test is pure policy (no network, KINORA_LIVE_VIDEO off): arc
tracking + time-travel resolution, tension/pacing curves and scoring, act/episode
boundary detection, budget-bounded recap selection, motif callback scheduling,
the richer weighed §7.2 arbitration, the cross-book bible, cross-volume conflict
detection, the pacing-driven re-plan signals, and the §13 coherence metrics. The
Showrunner's prose-only series methods are exercised with a JsonSequencer
stand-in. The existing single-book showrunner tests stay green (see
``test_agents_showrunner.py``) — proof the additive contract changes broke nothing.
"""

from __future__ import annotations

import pytest

from app.agents import series
from app.agents.contracts import (
    ArbitrationContext,
    ArcBeat,
    ArcStage,
    Beat,
    ConflictObject,
    ConflictOption,
    ConflictOptionSpec,
    Motif,
    MotifKind,
    RelationshipKind,
    ScenePlan,
    ScenePlanItem,
    SeriesBible,
    TextualSupport,
    Volume,
)
from app.agents.series.continuity import PriorFact, ProposedFact
from app.agents.showrunner import Showrunner, decide_arbitration
from app.providers import Providers
from tests.test_agents_support import JsonSequencer, providers  # noqa: F401

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _arc_beats(volume: int = 0) -> list[ArcBeat]:
    """A clean rising→climax→resolution arc within one volume."""
    return [
        ArcBeat(volume_index=volume, beat_index=0, stage=ArcStage.SETUP, intensity=0.2),
        ArcBeat(volume_index=volume, beat_index=4, stage=ArcStage.RISING, intensity=0.5),
        ArcBeat(volume_index=volume, beat_index=8, stage=ArcStage.TURN, intensity=0.7),
        ArcBeat(volume_index=volume, beat_index=12, stage=ArcStage.CLIMAX, intensity=0.95),
        ArcBeat(volume_index=volume, beat_index=16, stage=ArcStage.RESOLUTION, intensity=0.3),
    ]


def _conflict(*, with_evolve: bool = True, user_facing: bool = True) -> ConflictObject:
    options = [
        ConflictOptionSpec(id=ConflictOption.HONOR_CANON, action="regenerate", cost_video_s=5.0),
        ConflictOptionSpec(id=ConflictOption.SURFACE_TO_USER, action="ask", cost_video_s=0.0),
    ]
    if with_evolve:
        options.append(
            ConflictOptionSpec(
                id=ConflictOption.EVOLVE_CANON, action="assert", requires="textual support"
            )
        )
    return ConflictObject(
        conflict_id="cf_001",
        raised_by="continuity_supervisor",
        claim="shot depicts the heroine drawing a sword",
        canon_fact="state_hero_sword_001 retired at beat_0034",
        user_facing=user_facing,
        options=options,
    )


# --------------------------------------------------------------------------- #
# Arc tracking (§7, §8.5)
# --------------------------------------------------------------------------- #


def test_arc_advances_monotonically_through_stages() -> None:
    arc = series.build_character_arc(entity_key="char_hero_001", beats=_arc_beats())
    state = series.current_arc_state(arc)
    assert state.stage is ArcStage.RESOLUTION
    assert state.intensity == pytest.approx(0.3)  # tracks the LAST beat, not the peak
    assert state.beats_seen == 5
    assert series.is_monotonic(arc)


def test_arc_state_at_is_a_time_travel_read() -> None:
    arc = series.build_character_arc(entity_key="char_hero_001", beats=_arc_beats())
    # As of beat 8 the arc has only reached the turn — never the future climax.
    at_turn = series.arc_state_at(arc, volume_index=0, beat_index=8)
    assert at_turn.stage is ArcStage.TURN
    assert at_turn.intensity == pytest.approx(0.7)
    # As of beat 0, only setup.
    at_start = series.arc_state_at(arc, volume_index=0, beat_index=0)
    assert at_start.stage is ArcStage.SETUP


def test_arc_stage_never_rewinds_even_with_an_earlier_stage_beat() -> None:
    beats = [
        ArcBeat(volume_index=0, beat_index=0, stage=ArcStage.CLIMAX, intensity=0.9),
        ArcBeat(volume_index=1, beat_index=0, stage=ArcStage.RISING, intensity=0.5),
    ]
    arc = series.build_character_arc(entity_key="x", beats=beats)
    state = series.current_arc_state(arc)
    # The resolved stage stays at the high-water mark (climax); intensity tracks last.
    assert state.stage is ArcStage.CLIMAX
    assert state.intensity == pytest.approx(0.5)
    # ...and the regression is reported, not silently applied.
    regressions = series.arc_regressions(arc)
    assert len(regressions) == 1
    assert regressions[0].stage is ArcStage.RISING
    assert not series.is_monotonic(arc)


def test_relationship_arc_canonicalizes_the_pair() -> None:
    arc = series.build_relationship_arc(
        entity_a="b_char",
        entity_b="a_char",
        kind=RelationshipKind.RIVAL,
        beats=_arc_beats(),
    )
    assert arc.entity_keys == ("a_char", "b_char")
    assert arc.kind is RelationshipKind.RIVAL


def test_merge_arc_beats_is_idempotent_on_position() -> None:
    base = _arc_beats()
    # Re-ingesting the same beats plus a new one yields no duplicates.
    extra = [ArcBeat(volume_index=1, beat_index=0, stage=ArcStage.SETUP, intensity=0.4)]
    merged = series.merge_arc_beats(base, [*base, *extra])
    positions = [(b.volume_index, b.beat_index) for b in merged]
    assert len(positions) == len(set(positions)) == len(base) + 1


def test_stage_progress_spans_zero_to_one() -> None:
    setup = series.fold_arc(
        [ArcBeat(stage=ArcStage.SETUP, intensity=0.1)]
    )
    assert series.stage_progress(setup) == pytest.approx(0.0)
    done = series.fold_arc(_arc_beats())
    assert series.stage_progress(done) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Pacing curves (§7)
# --------------------------------------------------------------------------- #


def test_tension_curve_peaks_at_the_climax() -> None:
    curve = series.tension_curve(_arc_beats())
    # The climax beat (index 3 in series order) has the highest tension.
    assert curve.peak_index == 3
    assert curve.points[3].tension == pytest.approx(0.95)  # climax weight = 1.0
    assert 0.0 < curve.mean_tension < 1.0


def test_pacing_score_rewards_a_well_shaped_curve() -> None:
    good = series.curve_from_tensions([0.1, 0.25, 0.4, 0.6, 0.8, 0.95, 0.4])
    flat = series.curve_from_tensions([0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5])
    early_peak = series.curve_from_tensions([0.95, 0.8, 0.6, 0.4, 0.3, 0.2, 0.1])
    assert series.pacing_score(good) > series.pacing_score(flat)
    assert series.pacing_score(good) > series.pacing_score(early_peak)


def test_flat_curve_is_detected_as_monotony() -> None:
    flat = series.curve_from_tensions([0.5, 0.5, 0.5, 0.5, 0.5])
    assert flat.monotony_runs
    assert series.monotony_fraction(flat) == pytest.approx(1.0)
    assert series.longest_flat_run(flat) == 5


def test_worst_pacing_window_finds_the_dull_stretch() -> None:
    # A dip in the middle: samples 2..4 are the lowest-energy window.
    curve = series.curve_from_tensions([0.8, 0.7, 0.1, 0.1, 0.1, 0.7, 0.9])
    win = series.worst_pacing_window(curve, window=3)
    assert win == (2, 4)


def test_worst_pacing_window_none_when_curve_too_short() -> None:
    curve = series.curve_from_tensions([0.5, 0.6])
    assert series.worst_pacing_window(curve, window=4) is None


def test_peak_position_is_a_fraction() -> None:
    late = series.curve_from_tensions([0.1, 0.2, 0.3, 0.9])
    assert series.peak_position(late) == pytest.approx(1.0)
    early = series.curve_from_tensions([0.9, 0.3, 0.2, 0.1])
    assert series.peak_position(early) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Structure detection (§7)
# --------------------------------------------------------------------------- #


def test_detect_act_boundaries_splits_a_three_act_curve() -> None:
    # Rise, fall, rise, fall — two clear act inflections.
    curve = series.curve_from_tensions(
        [0.2, 0.5, 0.8, 0.5, 0.2, 0.5, 0.85, 0.95, 0.3]
    )
    acts = series.detect_act_boundaries(curve, target_acts=3)
    kinds = {b.kind for b in acts}
    assert any(k == "act" for k in kinds)
    # Boundaries are in reading order.
    assert acts == sorted(acts, key=lambda b: b.at_beat)


def test_detect_episode_boundaries_end_on_cliffhangers() -> None:
    curve = series.curve_from_tensions(
        [0.2, 0.6, 0.9, 0.4, 0.3, 0.7, 0.95, 0.5, 0.4, 0.8, 0.99]
    )
    episodes = series.detect_episode_boundaries(curve, target_episodes=3)
    assert len(episodes) == 3
    # All but the last end on a cliffhanger; the last closes the volume.
    assert all(ep.cliffhanger for ep in episodes[:-1])
    assert episodes[-1].cliffhanger is False
    # Episodes tile the curve with no gaps.
    for a, b in zip(episodes, episodes[1:], strict=False):
        assert b.beat_start == a.beat_end + 1


def test_single_episode_when_target_is_one() -> None:
    curve = series.curve_from_tensions([0.2, 0.6, 0.9, 0.4])
    episodes = series.detect_episode_boundaries(curve, target_episodes=1)
    assert len(episodes) == 1
    assert episodes[0].cliffhanger is False


def test_assign_acts_to_beats_partitions_correctly() -> None:
    curve = series.curve_from_tensions([0.2, 0.5, 0.9, 0.4, 0.2, 0.7, 0.95, 0.3])
    acts = series.detect_act_boundaries(curve, target_acts=3)
    mapping = series.assign_acts_to_beats(acts, beat_indices=list(range(8)))
    # Beats before the first act break are act 1; acts only ever increase.
    values = [mapping[i] for i in range(8)]
    assert values[0] == 1
    assert values == sorted(values)


# --------------------------------------------------------------------------- #
# Motif planning (§7)
# --------------------------------------------------------------------------- #


def test_plan_motif_callbacks_plants_echoes_and_pays_off() -> None:
    motif = Motif(
        motif_id="m_sword",
        label="the lost sword",
        planted_volume=0,
        planted_beat=2,
        payoff_volumes=[2],
    )
    counts = {0: 20, 1: 20, 2: 20}
    callbacks = series.plan_motif_callbacks(motif, volume_beat_counts=counts, echoes_per_volume=1)
    kinds = [c.kind for c in callbacks]
    assert kinds[0] is MotifKind.PLANT
    assert MotifKind.ECHO in kinds
    assert kinds[-1] is MotifKind.PAYOFF
    # The payoff lands near the end of volume 2.
    payoff = next(c for c in callbacks if c.kind is MotifKind.PAYOFF)
    assert payoff.volume_index == 2
    assert payoff.beat_index >= 16


def test_due_callbacks_fires_at_position() -> None:
    motif = Motif(motif_id="m1", planted_volume=0, planted_beat=0, payoff_volumes=[1])
    callbacks = series.plan_motif_callbacks(
        motif, volume_beat_counts={0: 10, 1: 10}, echoes_per_volume=1
    )
    plant = series.due_callbacks(callbacks, volume_index=0, beat_index=0)
    assert plant and plant[0].kind is MotifKind.PLANT
    none_here = series.due_callbacks(callbacks, volume_index=0, beat_index=7)
    assert none_here == []
    windowed = series.due_callbacks(callbacks, volume_index=0, beat_index=6, window=2)
    assert windowed  # the volume-0 echo at beat 5 is within the window


def test_unresolved_motifs_flags_a_planted_motif_with_no_payoff() -> None:
    paid = Motif(motif_id="paid", planted_volume=0, planted_beat=0, payoff_volumes=[1])
    dangling = Motif(motif_id="dangling", planted_volume=0, planted_beat=1, payoff_volumes=[])
    callbacks = series.plan_all_callbacks(
        [paid, dangling], volume_beat_counts={0: 10, 1: 10}
    )
    unresolved = series.unresolved_motifs([paid, dangling], callbacks)
    assert unresolved == ["dangling"]


# --------------------------------------------------------------------------- #
# Recap selection (§7, §11)
# --------------------------------------------------------------------------- #


def _prior_beats() -> list[ArcBeat]:
    vol0 = [
        ArcBeat(volume_index=0, beat_index=i, intensity=0.1 + 0.1 * (i % 5), summary=f"v0b{i}")
        for i in range(10)
    ]
    vol1 = [
        ArcBeat(volume_index=1, beat_index=i, intensity=0.2 + 0.08 * (i % 6), summary=f"v1b{i}")
        for i in range(10)
    ]
    return vol0 + vol1


def test_select_recap_beats_respects_the_budget() -> None:
    spec = series.select_recap_beats(_prior_beats(), for_volume=2, budget_s=9.0)
    # 3s per beat, 9s budget -> at most 3 beats.
    assert len(spec.items) == 3
    assert spec.total_target_s == pytest.approx(9.0)
    # Only prior-volume beats are eligible.
    assert all(item.volume_index < 2 for item in spec.items)
    # Items are returned in reading order for playback.
    keys = [(i.volume_index, i.beat_index) for i in spec.items]
    assert keys == sorted(keys)


def test_select_recap_beats_empty_when_no_prior_volumes() -> None:
    spec = series.select_recap_beats(_prior_beats(), for_volume=0, budget_s=30.0)
    assert spec.items == []


def test_recap_weighting_prefers_recent_high_intensity_beats() -> None:
    beats = [
        ArcBeat(volume_index=0, beat_index=0, intensity=0.1, summary="old low"),
        ArcBeat(volume_index=1, beat_index=9, intensity=0.95, summary="recent high"),
    ]
    spec = series.select_recap_beats(beats, for_volume=2, budget_s=3.0)
    assert len(spec.items) == 1
    assert spec.items[0].summary == "recent high"


def test_recap_includes_motif_planting_beats() -> None:
    beats = _prior_beats()
    motif = Motif(motif_id="m", planted_volume=0, planted_beat=3, payoff_volumes=[2])
    spec = series.select_recap_beats(beats, for_volume=2, budget_s=30.0, motifs=[motif])
    flagged = [item for item in spec.items if item.motif_ids]
    assert any(item.beat_index == 3 and item.volume_index == 0 for item in flagged)


# --------------------------------------------------------------------------- #
# Richer arbitration (§7.2)
# --------------------------------------------------------------------------- #


def test_weigh_arbitration_never_evolves_without_textual_support() -> None:
    ctx = ArbitrationContext(dramatic_stakes=0.9, in_climax=True)
    decision = series.weigh_arbitration(
        _conflict(), ctx, textual_support=False, director_present=True
    )
    assert decision.chosen_option is not ConflictOption.EVOLVE_CANON


def test_weigh_arbitration_evolves_with_textual_support() -> None:
    ctx = ArbitrationContext(dramatic_stakes=0.5)
    decision = series.weigh_arbitration(
        _conflict(), ctx, textual_support=True, director_present=False
    )
    assert decision.chosen_option is ConflictOption.EVOLVE_CANON
    assert decision.evolved_canon is True


def test_weigh_arbitration_recommends_surface_at_high_stakes() -> None:
    ctx = ArbitrationContext(dramatic_stakes=0.95, in_climax=True, arc_continuity_weight=0.1)
    decision = series.weigh_arbitration(
        _conflict(), ctx, textual_support=False, director_present=True
    )
    assert decision.recommended_option is ConflictOption.SURFACE_TO_USER
    assert decision.scores["surface_to_user"] > decision.scores["honor_canon"]


def test_weigh_arbitration_honors_when_continuity_dominates() -> None:
    ctx = ArbitrationContext(dramatic_stakes=0.1, in_climax=False, arc_continuity_weight=0.95)
    decision = series.weigh_arbitration(
        _conflict(), ctx, textual_support=False, director_present=False
    )
    assert decision.chosen_option is ConflictOption.HONOR_CANON
    assert decision.recommended_option is ConflictOption.HONOR_CANON


def test_decide_arbitration_context_is_backward_compatible() -> None:
    # Without a context, behaviour is the original three-branch gate.
    chosen, evolved = decide_arbitration(
        _conflict(), textual_support=True, director_present=False
    )
    assert chosen is ConflictOption.EVOLVE_CANON
    assert evolved is True
    # The plain (contextless) honor default still holds.
    chosen, _ = decide_arbitration(
        _conflict(), textual_support=False, director_present=False
    )
    assert chosen is ConflictOption.HONOR_CANON


def test_decide_arbitration_with_context_upgrades_to_surface() -> None:
    ctx = ArbitrationContext(dramatic_stakes=0.95, in_climax=True, arc_continuity_weight=0.1)
    chosen, _ = decide_arbitration(
        _conflict(), textual_support=False, director_present=True, context=ctx
    )
    assert chosen is ConflictOption.SURFACE_TO_USER


# --------------------------------------------------------------------------- #
# Series bible (§7, §8.1)
# --------------------------------------------------------------------------- #


def _bible() -> SeriesBible:
    volumes = [
        Volume(volume_index=0, title="Book I", beat_count=20),
        Volume(volume_index=1, title="Book II", beat_count=20),
        Volume(volume_index=2, title="Book III", beat_count=20),
    ]
    hero_beats = [
        ArcBeat(volume_index=0, beat_index=2, stage=ArcStage.SETUP, intensity=0.2),
        ArcBeat(volume_index=1, beat_index=5, stage=ArcStage.RISING, intensity=0.6),
        ArcBeat(volume_index=2, beat_index=18, stage=ArcStage.CLIMAX, intensity=0.95),
    ]
    return series.build_series_bible(
        series_id="ser_1",
        title="The Saga",
        volumes=volumes,
        character_beats={"char_hero_001": hero_beats},
        character_names={"char_hero_001": "Hero"},
        relationship_beats={("char_hero_001", "char_rival_002"): hero_beats},
        relationship_kinds={("char_hero_001", "char_rival_002"): RelationshipKind.RIVAL},
        motifs=[
            Motif(
                motif_id="m1",
                label="dawn",
                planted_volume=0,
                planted_beat=1,
                payoff_volumes=[2],
            )
        ],
    )


def test_build_series_bible_assembles_arcs_and_volumes() -> None:
    bible = _bible()
    assert len(bible.volumes) == 3
    assert len(bible.character_arcs) == 1
    arc = series.character_arc(bible, "char_hero_001")
    assert arc is not None
    assert arc.spanned_volumes == [0, 1, 2]


def test_bible_character_arc_state_is_time_travel() -> None:
    bible = _bible()
    # As of volume 1, the hero has only reached rising — not the v2 climax.
    state = series.character_arc_state_at(
        bible, entity_key="char_hero_001", volume_index=1, beat_index=10
    )
    assert state is not None
    assert state.stage is ArcStage.RISING


def test_bible_motifs_due_at_payoff_position() -> None:
    bible = _bible()
    # The motif pays off near the end of volume 2 (beat 18).
    due = series.motifs_due_at(bible, volume_index=2, beat_index=18, window=2)
    assert any(cb.kind is MotifKind.PAYOFF for cb in due)


def test_bible_relationships_and_entities() -> None:
    bible = _bible()
    rels = series.relationships_of(bible, "char_hero_001")
    assert len(rels) == 1 and rels[0].kind is RelationshipKind.RIVAL
    assert "char_hero_001" in series.entities_in_volume(bible, 0)


def test_merge_into_bible_adds_new_volume_beats() -> None:
    bible = _bible()
    new = [ArcBeat(volume_index=2, beat_index=19, stage=ArcStage.RESOLUTION, intensity=0.4)]
    merged = series.merge_into_bible(bible, entity_key="char_hero_001", new_beats=new)
    arc = series.character_arc(merged, "char_hero_001")
    assert arc is not None
    assert series.current_arc_state(arc).stage is ArcStage.RESOLUTION


# --------------------------------------------------------------------------- #
# Cross-volume continuity (§7.2, §8.5)
# --------------------------------------------------------------------------- #


def test_cross_volume_conflict_detected() -> None:
    ledger = [
        PriorFact("char_a", "status", "deceased", established_volume=0),
    ]
    proposed = ProposedFact("char_a", "status", "alive", volume_index=2, beat_index=5)
    conflict = series.detect_cross_volume_conflict(proposed, ledger, conflict_id="xvc_1")
    assert conflict is not None
    assert conflict.prior_volume_index == 0
    assert conflict.current_volume_index == 2


def test_retired_prior_fact_does_not_constrain() -> None:
    ledger = [
        PriorFact("char_a", "status", "deceased", established_volume=0, retired=True),
    ]
    proposed = ProposedFact("char_a", "status", "alive", volume_index=2)
    assert series.detect_cross_volume_conflict(proposed, ledger, conflict_id="x") is None


def test_same_value_is_not_a_contradiction() -> None:
    ledger = [PriorFact("char_a", "location", "north", established_volume=0)]
    proposed = ProposedFact("char_a", "location", "north", volume_index=1)
    assert series.detect_cross_volume_conflict(proposed, ledger, conflict_id="x") is None


def test_scan_cross_volume_returns_all_hits() -> None:
    ledger = [
        PriorFact("a", "status", "dead", established_volume=0),
        PriorFact("b", "weapon", "none", established_volume=0),
    ]
    proposed = [
        ProposedFact("a", "status", "alive", volume_index=1),
        ProposedFact("b", "weapon", "sword", volume_index=1),
        ProposedFact("c", "mood", "happy", volume_index=1),  # no prior, clean
    ]
    conflicts = series.scan_cross_volume(proposed, ledger)
    assert len(conflicts) == 2


def test_active_prior_facts_scopes_by_volume() -> None:
    ledger = [
        PriorFact("a", "x", "1", established_volume=0),
        PriorFact("b", "y", "2", established_volume=1, retired=True),
        PriorFact("c", "z", "3", established_volume=2),
    ]
    active = series.active_prior_facts(ledger, before_volume=2)
    keys = {f.subject_entity_key for f in active}
    assert keys == {"a"}  # b retired, c established at/after volume 2


# --------------------------------------------------------------------------- #
# Planner re-plan signals (§7)
# --------------------------------------------------------------------------- #


def _plan(n: int = 7) -> ScenePlan:
    return ScenePlan(
        scenes=[ScenePlanItem(scene_index=i, summary=f"scene {i}") for i in range(n)],
        volume_index=0,
    )


def test_annotate_plan_fills_tension_acts_and_curve() -> None:
    plan = _plan()
    tensions = {0: 0.1, 1: 0.3, 2: 0.6, 3: 0.4, 4: 0.7, 5: 0.95, 6: 0.3}
    annotated = series.annotate_plan(plan, scene_tensions=tensions)
    assert annotated.pacing_curve is not None
    assert all(s.tension is not None for s in annotated.scenes)
    assert all(s.act is not None for s in annotated.scenes)


def test_replan_directive_flags_a_flat_plan() -> None:
    plan = _plan()
    flat = dict.fromkeys(range(7), 0.5)
    annotated = series.annotate_plan(plan, scene_tensions=flat)
    directive = series.replan_directive(annotated, min_score=0.6)
    assert directive.needed is True
    assert directive.end_scene >= directive.start_scene
    assert directive.deficit >= 0.0


def test_replan_directive_passes_a_well_paced_plan() -> None:
    plan = _plan()
    good = {0: 0.1, 1: 0.25, 2: 0.4, 3: 0.55, 4: 0.7, 5: 0.95, 6: 0.4}
    annotated = series.annotate_plan(plan, scene_tensions=good)
    directive = series.replan_directive(annotated, min_score=0.5)
    assert directive.needed is False


def test_smooth_plan_tensions_lifts_a_window() -> None:
    tensions = [0.8, 0.7, 0.1, 0.1, 0.1, 0.7, 0.9]
    lifted = series.smooth_plan_tensions(tensions, (2, 4), lift=0.5)
    # The middle of the window is lifted the most.
    assert lifted[3] > tensions[3]
    before = series.pacing_score(series.curve_from_tensions(tensions))
    after = series.pacing_score(series.curve_from_tensions(lifted))
    assert after >= before


# --------------------------------------------------------------------------- #
# Eval metrics (§7, §13)
# --------------------------------------------------------------------------- #


def test_arc_coherence_full_for_monotonic_arcs() -> None:
    bible = _bible()
    report = series.arc_coherence(bible)
    assert report.coherence == pytest.approx(1.0)
    assert report.regressions == []


def test_arc_coherence_flags_a_regression() -> None:
    bad_beats = [
        ArcBeat(volume_index=0, beat_index=0, stage=ArcStage.CLIMAX, intensity=0.9),
        ArcBeat(volume_index=1, beat_index=0, stage=ArcStage.RISING, intensity=0.5),
    ]
    bible = series.build_series_bible(
        series_id="s",
        volumes=[Volume(volume_index=0), Volume(volume_index=1)],
        character_beats={"x": bad_beats},
    )
    report = series.arc_coherence(bible)
    assert report.coherence < 1.0
    assert "x" in report.regressions


def test_motif_resolution_reports_payoff_rate() -> None:
    paid = Motif(motif_id="paid", planted_volume=0, planted_beat=0, payoff_volumes=[1])
    dangling = Motif(motif_id="dangling", planted_volume=0, planted_beat=1, payoff_volumes=[])
    callbacks = series.plan_all_callbacks([paid, dangling], volume_beat_counts={0: 10, 1: 10})
    report = series.motif_resolution([paid, dangling], callbacks)
    assert report.payoff_rate == pytest.approx(0.5)
    assert report.unresolved == ["dangling"]


def test_series_health_blends_the_three_checks() -> None:
    bible = _bible()
    callbacks = series.motif_callbacks(bible)
    curve = series.tension_curve(_arc_beats())
    health = series.series_health(bible, volume_curves={0: curve}, callbacks=callbacks)
    assert set(health) == {"arc_coherence", "mean_pacing", "motif_payoff_rate", "overall"}
    assert 0.0 <= health["overall"] <= 1.0


# --------------------------------------------------------------------------- #
# Showrunner orchestration + prose synthesis (no network)
# --------------------------------------------------------------------------- #


def test_showrunner_plan_series_volume_annotates(providers: Providers) -> None:  # noqa: F811
    sr = Showrunner(providers)
    plan = _plan()
    tensions = {0: 0.1, 1: 0.3, 2: 0.6, 3: 0.4, 4: 0.7, 5: 0.95, 6: 0.3}
    annotated, directive = sr.plan_series_volume(plan, scene_tensions=tensions)
    assert annotated.pacing_curve is not None
    assert isinstance(directive.needed, bool)


def test_showrunner_build_recap_is_pure(providers: Providers) -> None:  # noqa: F811
    sr = Showrunner(providers)
    spec = sr.build_recap(_prior_beats(), for_volume=2, budget_s=9.0)
    assert len(spec.items) == 3
    assert spec.narration == ""  # prose not yet synthesized


def test_showrunner_weigh_conflict_exposes_scores(providers: Providers) -> None:  # noqa: F811
    sr = Showrunner(providers)
    ctx = ArbitrationContext(dramatic_stakes=0.9, in_climax=True, arc_continuity_weight=0.1)
    decision = sr.weigh_conflict(
        _conflict(), ctx, textual_support=False, director_present=True
    )
    assert decision.recommended_option is ConflictOption.SURFACE_TO_USER
    assert "surface_to_user" in decision.scores


async def test_showrunner_synthesize_recap_fills_narration(
    providers: Providers,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sr = Showrunner(providers)
    spec = sr.build_recap(_prior_beats(), for_volume=2, budget_s=9.0)
    monkeypatch.setattr(
        providers.chat,
        "chat_json",
        JsonSequencer({"narration": "Previously, the hero rose and faltered."}),
    )
    filled = await sr.synthesize_recap(spec)
    assert filled.narration.startswith("Previously")
    # The selection is unchanged — only the prose was added.
    assert [i.beat_index for i in filled.items] == [i.beat_index for i in spec.items]


async def test_showrunner_synthesize_recap_skips_empty(providers: Providers) -> None:  # noqa: F811
    sr = Showrunner(providers)
    empty = sr.build_recap(_prior_beats(), for_volume=0, budget_s=30.0)
    # No items -> no model call; returns unchanged.
    out = await sr.synthesize_recap(empty)
    assert out.items == []
    assert out.narration == ""


async def test_showrunner_synthesize_bible_synopsis(
    providers: Providers,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sr = Showrunner(providers)
    bible = _bible()
    monkeypatch.setattr(
        providers.chat,
        "chat_json",
        JsonSequencer({"synopsis": "A saga in three volumes about the Hero."}),
    )
    out = await sr.synthesize_bible_synopsis(bible)
    assert "saga" in out.synopsis.lower()


async def test_showrunner_arbitrate_with_context_carries_scores(
    providers: Providers,  # noqa: F811
) -> None:
    sr = Showrunner(providers)
    ctx = ArbitrationContext(dramatic_stakes=0.95, in_climax=True, arc_continuity_weight=0.1)
    record = await sr.arbitrate(
        _conflict(),
        source_span_text="she reached the bank empty-handed",
        director_present=True,
        textual_support=TextualSupport(supported=False, reasoning="no reacquisition"),
        context=ctx,
    )
    assert record.recommended_option is ConflictOption.SURFACE_TO_USER
    assert record.scores  # scored decision is carried for the feed
    assert record.reasoning


# --------------------------------------------------------------------------- #
# Arc-beat inference (§7, §8.4) — the read model, no new ingest
# --------------------------------------------------------------------------- #


def test_mood_intensity_lexicon() -> None:
    assert series.mood_intensity("calm") < series.mood_intensity("tense")
    assert series.mood_intensity("very climactic moment") == pytest.approx(0.95)
    # Unknown mood -> the neutral default.
    assert series.mood_intensity("indescribable") == pytest.approx(0.4)
    assert series.mood_intensity(None) == pytest.approx(0.4)


def test_cue_boost_from_action_words() -> None:
    assert series.cue_boost("a quiet morning") == pytest.approx(0.0)
    assert series.cue_boost("the battle and the betrayal") > 0.0
    # Capped.
    assert series.cue_boost("battle fight death betray escape chase confront") <= 0.25


def test_stage_for_position_is_monotone_through_the_arc() -> None:
    stages = [series.stage_for_position(p / 10) for p in range(11)]
    ranks = [series.stage_rank(s) for s in stages]
    assert ranks == sorted(ranks)  # position only ever advances the stage
    assert stages[0] is ArcStage.SETUP
    assert stages[-1] is ArcStage.RESOLUTION


def test_infer_arc_from_beats_builds_a_well_shaped_arc() -> None:
    beats = [
        Beat(beat_index=0, summary="a calm opening", mood="calm"),
        Beat(beat_index=1, summary="rising unease", mood="tense"),
        Beat(beat_index=2, summary="the great battle and a sacrifice", mood="violent"),
        Beat(beat_index=3, summary="a quiet farewell", mood="wistful"),
    ]
    arc_beats = series.infer_arc_from_beats(beats, volume_index=1)
    assert len(arc_beats) == 4
    assert all(b.volume_index == 1 for b in arc_beats)
    # The violent battle beat is the hottest.
    assert max(arc_beats, key=lambda b: b.intensity).summary.startswith("the great battle")
    # Stages advance with position.
    ranks = [series.stage_rank(b.stage) for b in arc_beats]
    assert ranks == sorted(ranks)


def test_infer_character_arc_across_volumes_is_monotonic() -> None:
    # A character whose arc spans three volumes should advance, not reset per book.
    volume_beats = {
        v: [Beat(beat_index=i, summary=f"v{v}b{i}", mood="calm") for i in range(10)]
        for v in range(3)
    }
    arc_beats = series.infer_character_arc_across_volumes(volume_beats)
    arc = series.build_character_arc(entity_key="hero", beats=arc_beats)
    assert series.is_monotonic(arc)
    # Opens in setup, ends in resolution.
    assert arc_beats[0].stage is ArcStage.SETUP
    assert series.current_arc_state(arc).stage is ArcStage.RESOLUTION
    # Volume 0 holds the early stages; volume 2 holds the late ones.
    vol0_stages = {series.stage_rank(b.stage) for b in arc_beats if b.volume_index == 0}
    vol2_stages = {series.stage_rank(b.stage) for b in arc_beats if b.volume_index == 2}
    assert max(vol0_stages) <= min(vol2_stages)


def test_infer_character_arc_across_volumes_empty() -> None:
    assert series.infer_character_arc_across_volumes({}) == []
    assert series.infer_character_arc_across_volumes({0: []}) == []


def test_infer_scene_tensions_prefers_explicit_then_mood_then_position() -> None:
    scenes = [
        ScenePlanItem(scene_index=0, summary="opening", tension=0.9),  # explicit wins
        ScenePlanItem(scene_index=1, summary="a tense confrontation"),  # mood lookup
        ScenePlanItem(scene_index=2, summary="filler"),  # positional default
    ]
    tensions = series.infer_scene_tensions(scenes, moods={1: "tense"})
    assert tensions[0] == pytest.approx(0.9)
    assert tensions[1] > 0.5  # tense + a confront cue
    assert 0.0 <= tensions[2] <= 1.0


# --------------------------------------------------------------------------- #
# End-to-end series assembly (§7)
# --------------------------------------------------------------------------- #


def _three_volume_inputs() -> tuple[list[Volume], dict[int, list[ArcBeat]]]:
    volumes = [
        Volume(volume_index=i, title=f"Book {i + 1}", beat_count=12) for i in range(3)
    ]
    arc_beats: dict[int, list[ArcBeat]] = {}
    for v in range(3):
        beats = [
            Beat(beat_index=i, summary=f"v{v} beat {i}", mood="tense" if i % 4 == 2 else "calm")
            for i in range(12)
        ]
        arc_beats[v] = series.infer_arc_from_beats(beats, volume_index=v)
    return volumes, arc_beats


def test_assemble_series_produces_a_full_plan() -> None:
    volumes, arc_beats = _three_volume_inputs()
    motif = Motif(
        motif_id="m_dawn", label="dawn", planted_volume=0, planted_beat=0, payoff_volumes=[2]
    )
    # The cross-book hero arc must use a series-global progression, not naive
    # per-volume concatenation, so it advances monotonically across the trilogy.
    hero_beats = series.infer_character_arc_across_volumes(
        {
            v: [Beat(beat_index=i, summary=f"v{v}b{i}", mood="calm") for i in range(12)]
            for v in range(3)
        }
    )
    plan = series.assemble_series(
        series_id="ser_x",
        title="The Trilogy",
        volumes=volumes,
        volume_arc_beats=arc_beats,
        character_arc_beats={"char_hero": hero_beats},
        character_names={"char_hero": "Hero"},
        motifs=[motif],
    )
    assert plan.bible.series_id == "ser_x"
    assert len(plan.volume_structures) == 3
    # Each volume has a curve + structure.
    for vs in plan.volume_structures:
        assert vs.pacing_curve.points
        assert isinstance(vs.pacing_score, float)
    # Motif callbacks were scheduled and the motif pays off.
    assert any(cb.kind is MotifKind.PAYOFF for cb in plan.motif_callbacks)
    # Recaps for volumes 1 and 2 (none for the first).
    assert {r.for_volume for r in plan.recaps} == {1, 2}
    assert all(r.total_target_s <= 12.0 + 1e-9 for r in plan.recaps)
    # Health scoreboard present, and the coherent cross-book arc scores full marks.
    assert set(plan.health) >= {"arc_coherence", "motif_payoff_rate", "overall"}
    assert plan.health["arc_coherence"] == pytest.approx(1.0)


def test_plan_summary_is_feed_ready() -> None:
    volumes, arc_beats = _three_volume_inputs()
    plan = series.assemble_series(
        series_id="ser_y",
        volumes=volumes,
        volume_arc_beats=arc_beats,
        character_arc_beats={"char_hero": arc_beats[0]},
    )
    summary = series.plan_summary(plan)
    assert summary["series_id"] == "ser_y"
    assert summary["volumes"] == 3
    assert "overall_health" in summary


# --------------------------------------------------------------------------- #
# Showrunner series orchestration methods (no network where pure)
# --------------------------------------------------------------------------- #


def test_showrunner_assemble_is_pure(providers: Providers) -> None:  # noqa: F811
    sr = Showrunner(providers)
    volumes, arc_beats = _three_volume_inputs()
    plan = sr.assemble(
        series_id="ser_z",
        title="Saga",
        volumes=volumes,
        volume_arc_beats=arc_beats,
        character_arc_beats={"char_hero": arc_beats[0]},
    )
    assert plan.bible.series_id == "ser_z"
    assert len(plan.volume_structures) == 3


async def test_showrunner_replan_for_pacing_passes_a_good_plan(
    providers: Providers,  # noqa: F811
) -> None:
    sr = Showrunner(providers)
    plan = _plan()
    good = {0: 0.1, 1: 0.25, 2: 0.4, 3: 0.55, 4: 0.7, 5: 0.95, 6: 0.4}
    # A well-paced plan needs no model call; the annotated plan comes straight back.
    out = await sr.replan_for_pacing(plan, scene_tensions=good, min_pacing_score=0.5)
    assert out.pacing_curve is not None


async def test_showrunner_replan_for_pacing_calls_model_on_flat_plan(
    providers: Providers,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sr = Showrunner(providers)
    plan = _plan()
    flat = dict.fromkeys(range(7), 0.5)
    # The model returns a revised plan; we stub it so no network is touched.
    revised_scenes = {
        "scenes": [{"scene_index": i, "summary": f"revised {i}"} for i in range(7)]
    }
    monkeypatch.setattr(providers.chat, "chat_json", JsonSequencer(revised_scenes))
    out = await sr.replan_for_pacing(plan, scene_tensions=flat, min_pacing_score=0.6)
    assert out.pacing_curve is not None
    assert out.scenes[0].summary == "revised 0"
